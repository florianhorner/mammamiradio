# Changelog

All notable changes to `fakeitaliradio` are documented here.

The current version source of truth is `pyproject.toml`.

## [Unreleased]

### Added

- **Docker support**: `Dockerfile` and `docker-compose.yml` for running on any platform (Windows, Mac, Linux) without Python/FFmpeg setup.
- **Home Assistant add-on**: Full HA add-on scaffold in `ha-addon/` with ingress support, automatic HA token injection, and configurable options UI.
- **Ingress-compatible UI**: Dashboard and listener HTML now use dynamic base paths, working behind HA's ingress proxy and at the root path.
- **Environment variable overrides**: `config.py` accepts `HA_URL`, `HA_ENABLED`, `STATION_NAME`, `STATION_THEME`, `PLAYLIST_SPOTIFY_URL`, `CLAUDE_MODEL`, `FAKEITALIRADIO_CACHE_DIR`, and `FAKEITALIRADIO_TMP_DIR` for Docker/add-on configuration without editing `radio.toml`.
- **GitHub Actions Docker CI**: Multi-arch (amd64 + arm64) image builds on tag push, published to GHCR.
- `.dockerignore` for clean Docker builds.

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

- Start a local fake Italian radio station with an admin dashboard at `/`, a public listener page at `/listen`, and a raw MP3 stream at `/stream`.
- Alternate songs with AI-written host banter and multi-spot fake ad breaks, including bumper jingles, custom ad voices, and recurring campaign callbacks.
- Expose admin controls for shuffle, skip, queue purge, track removal, reordering, and "play next" from the web UI.
- Provide public station status plus admin-only logs and debugging details for queue depth, recent playback, generated scripts, and go-librespot output.

### Changed

- Prefer real Spotify playback through go-librespot when a user connects the `fakeitaliradio` device, but keep the station alive with liked songs, demo tracks, local files, yt-dlp, or placeholder audio when that path is unavailable.
- Throttle stream output to the configured bitrate so the dashboard, listener, and actual audio timeline stay aligned.
- Require admin auth when binding to a non-local interface, while keeping localhost development friction low.

### For contributors

- Add pytest coverage for config validation, scheduler pacing, ad-brand selection, campaign history, and ffmpeg-backed audio helpers.
- Ship a local dev entry point in `start.sh` plus template config in `.env.example` and `radio.toml`.
