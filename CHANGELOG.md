# Changelog

All notable changes to `mammamiradio` are documented here.

The current version source of truth is `pyproject.toml`.

## [1.5.1] - 2026-04-05

### Added

- **OpenAI TTS engine for hosts**: hosts can now use OpenAI's `gpt-4o-mini-tts` as an alternative to Edge TTS via `engine = "openai"` in `radio.toml`. Marco defaults to the `onyx` voice, giving him distinct vocal DNA from Giulia.
- **Personality-aware TTS instructions**: OpenAI voice synthesis uses host personality axes (energy, warmth, chaos) to shape delivery style, not just a static prompt.
- **Edge fallback voice**: hosts with `engine = "openai"` can specify `edge_fallback_voice` so they fall back to their original voice (not a stranger) when OpenAI is unavailable.
- **Singleton OpenAI client**: connection pool reused across all TTS calls for better latency.
- **HA addon OpenAI support**: `OPENAI_API_KEY` now included in addon secret hydration.

### Changed

- **Richer ad SFX**: cash register, whoosh, mandolin sting, and ice clink now use layered tones with exponential decay envelopes and noise transients instead of plain sine waves.
- **Better bumper jingles**: melody notes now have plucked envelopes and velocity variation over a sustained C-major pad, with a multi-tap echo tail.
- **Music beds with warmth**: all moods use 4 harmonic layers (root, third, detuned chorus, fifth) instead of 2 bare sines, with per-mood tremolo rates and reverb. Suspicious jazz has a walking bass line.
- **Punchier ad processing**: `normalize_ad()` uses heavier compression (ratio 8:1, -24dB threshold), presence + air boost, and a 120Hz mud cut for louder, brighter commercials that pop against music.
- **FFmpeg performance**: collapsed multi-input sine generators into single `aevalsrc` expressions across all SFX and music bed functions (up to 8 inputs reduced to 1), cutting filter graph overhead.

## [Unreleased]

### Added

- **Capability flags**: `Capabilities` frozen dataclass and `capabilities.py` replace the 64-state mode system. Four independent flags (`spotify_connected`, `spotify_api`, `anthropic`, `ha`) derive a tier label. `GET /api/capabilities` returns flags, tier, guided next-step hint, and connect status.
- **Demo-first boot**: station starts instantly with zero config. Built-in demo playlist of classic Italian tracks plays while you set up Spotify.
- **Shareware trial**: first 3 banter clips use pre-bundled audio. After the trial, the station prompts for an Anthropic API key to unlock live AI hosts.
- **Autoplay on Spotify Connect**: when a user taps the station in Spotify, the producer captures the currently playing song and generates personalized welcome banter referencing it, both in parallel.
- **`capture_current_audio()`**: captures audio already playing on go-librespot without issuing a `play_track` call.
- **`get_current_track()`**: reads current track metadata from go-librespot `/status` API.
- **Embedded dashboard player**: play button + auto-reconnect on hot reload. Stream starts on first click anywhere.
- **Listener persona system**: `persona.py` tracks aggregate listening patterns (skip rate, energy preference, ballad loyalty) and feeds them into banter prompts for eerily on-point host commentary.
- **Config sync module**: `sync.py` synchronizes go-librespot device name, FIFO path, and API port from `radio.toml` into the go-librespot config on startup.
- **Ephemeral segment flag**: `Segment.ephemeral` controls whether segment audio files are cleaned up after playback.

### Changed

- **Dashboard Volare theme**: warm Italian sunset palette — orange-red dominant background, deep sienna card surfaces, golden sun accent (#F4D048), Lancia-red FM dial needle with glow. Playfair Display italic station name. All JS/API logic preserved.
- **Dashboard redesigned**: 640px single-column, capability-driven cards, connect hero card, tier badge with pulse animation on upgrade.
- **Voice differentiation**: SSML prosody (rate/pitch) derived from personality axes. Marco → GianniNeural (booming), personality axes widened for contrast.
- **Louder ad breaks**: 6-note bumper jingle (1.5s, 1.8x volume), music bed 0.12→0.20, env bed 0.06→0.10.
- **go-librespot config sync**: `start.sh` syncs device name, FIFO path, and API port on startup.
- **Autoplay fade-in**: 300ms fade-in on captured audio smooths the handover from demo.
- **Sine wave fallback removed**: last-resort placeholder generates silence instead of 440Hz tone.
- **`start.sh` reload**: uvicorn now watches `*.html` files for hot reload.

### Fixed

- Auto-transfer no longer spams logs when no Spotify Client ID is configured.
- Banter generation no longer errors when no Anthropic key is set (early return with fallback text).
- Banter TTS failure gracefully skips the segment instead of crashing the producer loop.
- Spotify audio capture failures now fall back to local download instead of crashing the producer.
- `start.sh` reclaims stale ports on startup instead of failing with "address already in use".

## [1.5.0] - 2026-04-04

### Added

- **Signature Ad System**: ads are now a full creative sub-format with 6 ad formats (classic pitch, testimonial, duo scene, live remote, late-night whisper, institutional PSA), sonic worlds per category, role-based speaker casting, and per-brand campaign memory with escalation rules.
- 6 new SFX types (tape stop, hotline beep, mandolin sting, ice clink, startup synth, register hit) and 8 new music beds (tarantella pop, cheap synth romance, suspicious jazz, discount techno, plus environment beds for cafe, beach, showroom, stadium, and more).
- Brand motif generation: recurring brands get a short audio jingle built from their sonic signature, prepended to each ad spot.
- Environment bed layering: ads can have a quiet ambient bed (cafe noise, highway hum) mixed under the voice before the music bed.
- Multi-voice ad support: duo scenes and testimonials cast two distinct speakers with role-based voice resolution.
- Campaign spines in `radio.toml`: each recurring brand can define a premise, escalation rule, preferred format pool, sonic signature, and spokesperson.
- `concat_files` now properly inserts silence gaps between segments (previously the `silence_ms` parameter was accepted but unused).
- Spotify source picker: choose from your playlists or Liked Songs directly in the dashboard (local/macOS mode only).
- Persisted source selection: the station restores your last chosen source on restart via `cache/playlist_source.json`.
- New API routes: `GET /api/spotify/source-options` and `POST /api/spotify/source/select` for programmatic source switching.
- CSRF protection for admin mutating endpoints when accessed over non-loopback networks.
- Personality sliders in the dashboard for tuning host energy, chaos, warmth, verbosity, and nostalgia.
- Conductor workspace support with lifecycle scripts for multi-agent development.
- Dependabot automerge workflow for dependency updates.

### Changed

- Ad generation prompt rewritten around explicit format selection, speaker role descriptions, and sonic world cues instead of one generic prompt.
- `write_ad()` now accepts a voice dict (role->AdVoice) instead of a single voice, enabling multi-speaker ads.
- `synthesize_ad()` resolves voice per-part by role, with graceful fallback to the first voice in the dict.
- Ad history now tracks format and sonic signature alongside brand and summary, enabling format rotation and campaign continuity.
- LLM ad summaries are now instructed to be in English for consistent campaign arc tracking.
- SFX type list in the LLM prompt is now generated from a single source of truth (`AVAILABLE_SFX_TYPES` in normalizer.py).
- Source switching now triggers immediate cutover: queued segments are purged and current playback is skipped so the new source starts right away.
- Concurrent source switches are serialized so rapid clicks cannot corrupt station state.
- The producer detects source changes mid-generation and discards stale segments instead of queuing them.
- In addon/Docker mode, the interactive source picker is disabled server-side; use the playlist URL field instead.

### Fixed

- Category-based sonic world defaults no longer share mutable references across calls (prevented silent state corruption).
- Duo scenes and testimonials with only one role in the LLM output are demoted to classic pitch instead of producing broken multi-voice audio.
- `_estimate_duration` helper used consistently instead of inline formula duplication.
- Spotify playlist fetch returned zero tracks when API items were nested under `item` key.
- Source picker showed 0 tracks for user playlists due to wrong count field.
- Listener page `_base is not defined` JS error from service worker scope.
- Producer recovery stall when go-librespot restarts mid-segment.

## [1.2.0] - 2026-04-02

### Added

- Go-librespot runtime ownership helpers, config-sync support, and add-on smoke coverage for startup/config path handling.
- Unauthenticated `/healthz` and `/readyz` probes, Makefile-based local quality commands, and broader regression coverage across streamer, producer, Spotify, and audio normalization paths.
- A four-step first-run onboarding flow in the dashboard, backed by `/api/setup/status`, `/api/setup/recheck`, and `/api/setup/addon-snippet`, plus a persistent station-mode banner.

### Changed

- `start.sh` now reuses the owned go-librespot process when possible, and local dev picks up `LOG_LEVEL` plus `*.toml` reloads without extra shell wiring.
- Local quality checks now run through the repo `.venv` with coverage, timeout, watch, and CI-aligned tooling defaults instead of relying on global Python utilities.
- README and Home Assistant add-on docs now mirror the same four onboarding steps and setup language shown in the product UI.

### Fixed

- Spotify auto-transfer now uses the configured go-librespot device name, and runtime ownership checks no longer rely on loose `pgrep` matching.
- Apple Silicon and stripped-PATH installs now resolve `go-librespot` correctly during setup checks, and stale Spotify auth state no longer leaves the dashboard claiming the wrong station mode.
- Setup rechecks can use cached user Spotify auth for private playlists, and the sweep-regression tests now match the runtime chirp implementation shipped in the hardening work.

## [1.1.3] - 2026-04-03

### Added

- Native Conductor workspace lifecycle hooks via `conductor.json`, including repo-owned setup, run, and archive scripts for workspace bootstrapping and cleanup.

### Changed

- Contributor docs now explain that `conductor.json` and `scripts/conductor-*.sh` are committed repo infrastructure, while `.context/` stays runtime-only.

### Fixed

- Conductor workspace setup no longer depends on a bash snippet surviving interactive `zsh` execution, so new workspaces bootstrap the project venv reliably.

## [1.1.1] - 2026-04-02

### Changed

- Local guardrails now catch addon issues before you push. Pre-commit hooks enforce conventional commit messages and version sync. `./scripts/validate-addon.sh` runs 13+ checks (version parity, config wiring, Dockerfile safety, translations, ingress rewrite safety) so CI failures are caught locally first.

### Fixed

- Version-sync hook reads from the git index instead of the working tree, so partially staged commits can no longer bypass the version check.
- Addon validation script derives the image owner from the git remote (not `gh api user`), so contributors no longer get false image-path mismatches.
- Empty version parsing now fails fast with a clear error instead of silently passing.

### For contributors

- Regression tests for the staged-version hook and the ingress rewrite validator.
- `CLAUDE.md` commit prefix docs now match the hook allowlist (added `merge:`).

## [1.1.0] - 2026-04-01

### Added

- **PWA support**: Install Radio Italì as a mobile app from your browser. Manifest, service worker, and app icons included.
- **Lock screen controls**: MediaSession API integration shows song title, artist, and Radio Italì artwork on your lock screen. Play/pause works from lock screen and notification shade.
- **Install prompt**: Chromium browsers get a native install banner; iOS shows manual "Add to Home Screen" instructions.
- **Offline resilience**: Service worker caches the app shell. If you lose connection, you see an offline status and the stream auto-reconnects with exponential backoff.
- **Static file serving**: New `/static/` route serves PWA assets (manifest, icons) with path traversal protection. `/sw.js` served at root for service worker scope.
- **HA Ingress compatibility**: Service worker uses `endsWith`/`includes` path matching so caching works correctly behind Home Assistant's ingress proxy.
- Custom logo and favicon: retro radio with Italian flag stripe, Terracotta Sera palette
- Logo in READMEs, HA addon store listing (icon.png + logo.png), apple-touch-icon, OG tags
- go-librespot bundled in HA addon Docker image for Spotify Connect support
- HA addon runbook and addon-specific documentation

### Changed

- HA addon Dockerfile builds from local source (CI copies files into build context) instead of pip installing from git
- Improved producer segment production logging and error handling
- HA addon run.sh extracts all config options including claude_model and playlist_spotify_url

### Fixed

- Re-enabled `host_network: true` — required for go-librespot mDNS (Spotify Connect discovery). Admin endpoints protected by auto-generated `ADMIN_TOKEN`
- Fixed ingress double-prefix bug: `_inject_ingress_prefix` was rewriting JS string literals (e.g. `'/api/'`, `'/status'`) that the client-side `_base` variable already handles, causing all API requests to 404 behind HA ingress
- Sanitize HA entity state values before injecting into Claude prompts (truncate, filter injection patterns, wrap in data delimiters)
- Pin all GitHub Actions in addon-build.yml to SHA hashes (supply chain hardening)
- HA addon config.yaml schema uses `password?` type for secrets (masked in UI)

### For contributors

- 6 new tests: service worker route, manifest route, 404 for missing static files, path traversal blocked, ingress prefix rewriting for static paths and sw.js.

## [1.0.0] - 2026-03-30

### Added

- **Run with Docker**: `docker compose up` and you have a radio station. No Python, no FFmpeg, works on Windows, Mac, and Linux.
- **Home Assistant add-on**: Install from the HA add-on store, configure your API key, and the radio appears in your sidebar. Hosts automatically reference your home state (lights, temperature, who's home) with zero HA configuration.
- **Dashboard works behind HA ingress**: The web UI detects its base path at runtime, so it works both at `/` and behind Home Assistant's ingress proxy.
- **Configure via environment variables**: Override station name, Spotify playlist, Claude model, and HA settings without editing `radio.toml`. Useful for Docker and add-on deployments.
- **Multi-arch Docker CI**: GitHub Actions builds amd64 + arm64 images on tag push, published to GHCR.

### Changed

- Admin tokens are now accepted only via the `X-Radio-Admin-Token` header. Query parameter auth (`?admin_token=...`) removed to prevent token leakage in browser history, server logs, and referer headers.
- API error responses no longer expose internal exception details. Generic error messages are returned to clients while full details are logged server-side.

### For contributors

- `.dockerignore` for clean Docker builds.
- `tests/test_config_env_overrides.py` covers all new env-var override paths (11 tests).
- Pinned production dependency lockfile (`requirements.txt`) with SHA-256 hashes via `pip-compile`.
- All GitHub Actions SHA-pinned to immutable commit hashes across all 3 workflow files.
- Added `.github/CODEOWNERS` requiring review for workflow file changes.
- Added gitleaks pre-commit hook for secret scanning before push.

## [0.3.0] - 2026-03-30

### Added

- Home Assistant addon: run fakeitaliradio as a supervised addon with one-click install from the HA Add-on Store. Includes Dockerfile, config schema, and GitHub Actions build pipeline for amd64/aarch64.
- HACS custom integration: media player entity that auto-discovers the addon via the Supervisor API, shows current track info, and exposes the stream URL for relay to other speakers.
- Addon-aware config: automatic detection of HA environment via `SUPERVISOR_TOKEN`, persistent paths under `/data/`, and `options.json` secret injection.
- Ingress support: dashboard and listener pages rewrite URLs for HA's Ingress proxy, with auth handled by the Supervisor.
- Spotify headless auth: addon mode disables browser-based OAuth and falls back to client credentials for public playlist access when no cached user token exists.
- Tests for addon config detection, options parsing, ingress URL rewriting, auth bypass, and Spotify addon behavior.

### Changed

- `go_librespot_config_dir` is now configurable (addon uses `/data/go-librespot`).
- Non-local bind auth check skipped in addon mode (Supervisor handles auth).
- Log path references use `config.tmp_dir` instead of hardcoded `tmp/`.

## [0.2.0] - 2026-03-30

### Added

- One-click macOS launcher: `Start Radio.command` bootstraps venv, installs deps, starts the radio, and opens the dashboard. `Stop Radio.command` to stop.
- Search and add individual Spotify tracks from the dashboard.
- Load a new Spotify playlist by URL from the dashboard.
- Inline SVG favicon, OG tags, and `theme-color` on both listener and dashboard pages.
- Sharing instructions for listeners on your network in README.
- New API routes: `/api/search`, `/api/playlist/add`, `/api/playlist/load`.
- Ruff linting and formatting config in `pyproject.toml` with auto-fixed violations across all Python files.
- Mypy type checking config (lenient mode) to catch obvious type errors without blocking on untyped legacy code.
- Pre-commit hooks for ruff lint and format (runs on every commit).
- GitHub Actions CI workflow running ruff, mypy, and pytest on push/PR to main.
- Comprehensive test suite raising coverage from 30% to 63%.
- Automated dependency update infrastructure (Dependabot + merge workflow).
- `TODOS.md` for tracking deferred work items.

### Changed

- go-librespot default path changed from hardcoded Homebrew path to bare `go-librespot` (PATH lookup).
- Home Assistant disabled by default in `radio.toml` with empty URL.
- `start.sh` uses `.venv/bin/python` instead of system `python` for reliability.
- Dashboard layout widened to 1280px max-width with 3-column grid.
- "Purge Queue" renamed to "Clear Buffer" with descriptive tooltip.
- Station name corrected to "Radio Itali" with grave accent across listener page.
- Pinned ruff (0.9.10) and mypy (1.15.0) versions in CI to prevent surprise breakage from upstream releases.
- Updated `CONTRIBUTING.md` with lint, format, and type-check commands.

### Fixed

- Playlist URL placeholder mismatch that prevented auto-populating the current playlist URL.
- Playlist input stays permanently disabled if network request fails.
- Search shows "No results" instead of cryptic error when Spotify is not configured.
- Route documentation, placeholder text, and UI hardening fixes.

## [0.1.1] - 2026-03-29

### Added

- Dedicated repo docs for architecture, operations, troubleshooting, and contributing.
- Inline module, class, and function documentation across the Python application code.

### Changed

- Expanded `README.md` and `CLAUDE.md` so setup, auth, fallback behavior, and runtime flow match the current code.
- Kept `audio.bitrate` as the canonical bitrate setting in user-facing docs.

## [0.1.0] - 2026-03-29

### Added

- Start a local AI-powered Italian radio station with an admin dashboard at `/`, a public listener page at `/listen`, and a raw MP3 stream at `/stream`.
- Alternate songs with AI-written host banter and multi-spot AI-generated ad breaks, including bumper jingles, custom ad voices, and recurring campaign callbacks.
- Expose admin controls for shuffle, skip, queue purge, track removal, reordering, and "play next" from the web UI.
- Provide public station status plus admin-only logs and debugging details for queue depth, recent playback, generated scripts, and go-librespot output.

### Changed

- Prefer real Spotify playback through go-librespot when a user connects the `mammamiradio` device, but keep the station alive with liked songs, demo tracks, local files, yt-dlp, or placeholder audio when that path is unavailable.
- Throttle stream output to the configured bitrate so the dashboard, listener, and actual audio timeline stay aligned.
- Require admin auth when binding to a non-local interface, while keeping localhost development friction low.

### For contributors

- Add pytest coverage for config validation, scheduler pacing, ad-brand selection, campaign history, and ffmpeg-backed audio helpers.
- Ship a local dev entry point in `start.sh` plus template config in `.env.example` and `radio.toml`.
