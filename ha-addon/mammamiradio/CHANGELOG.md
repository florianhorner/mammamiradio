# Changelog

## 1.1.2

### Added

- The dashboard now ships a four-step first-run setup flow for add-on installs, including a copy-ready add-on configuration snippet and an explicit station-mode banner.
- `/healthz` and `/readyz` can now be used as add-on liveness/readiness probes instead of scraping the full admin status payload.

### Changed

- Add-on startup wiring now syncs the runtime config path and reuses the owned go-librespot process when it already matches the current Supervisor config.
- Add-on documentation now mirrors the same onboarding steps and labels shown in the dashboard, so setup instructions do not drift between UI and docs.

### Fixed

- Add-on setup checks now resolve the default Apple Silicon Homebrew `go-librespot` path correctly when PATH is sparse.
- Spotify setup rechecks clear stale connection state and can use cached user auth when probing playlists, so the add-on reports `Demo Mode`, `Degraded`, and `Real Spotify Mode` more accurately.

## 1.1.1

- Initial Home Assistant add-on release
- One-click install with ingress (sidebar) support
- Automatic Home Assistant state integration via Supervisor API
- Configurable Anthropic API key, Spotify credentials, and station name
- Falls back gracefully without Spotify or Anthropic credentials
