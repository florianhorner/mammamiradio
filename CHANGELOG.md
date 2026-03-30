# Changelog

All notable changes to `mammamiradio` are documented here.

The current version source of truth is `pyproject.toml`.

## [Unreleased]

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
