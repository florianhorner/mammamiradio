# Changelog

## 1.5.1

### Changed

- Internal data structures use bounded deques for automatic memory management.
- Home Assistant polling uses a reusable HTTP client (fewer connections, faster polls).

### Fixed

- Status API no longer crashes when serializing internal data structures.

## 1.5.0

### Added

- Signature Ad System: 6 ad formats, sonic worlds, role-based speaker casting, campaign memory with escalation rules, brand motifs, and environment beds.
- Multi-voice ad support for duo scenes and testimonials.
- Source switching now triggers immediate cutover with queue purge and playback skip.
- CSRF protection for admin endpoints accessed over non-loopback networks.

### Changed

- Ad generation uses format-specific prompts with sonic world cues and role-based voice casting.
- Setup status now accurately reflects configured Spotify credentials and active source state.

### Fixed

- Category sonic world defaults no longer share mutable references across calls.
- Spotify playlist fetch returned zero tracks when API items were nested under `item` key.
- Producer recovery stall when go-librespot restarts mid-segment.

## 1.2.0

### Added

- The dashboard now ships a four-step first-run setup flow for add-on installs, including a copy-ready add-on configuration snippet and an explicit station-mode banner.
- `/healthz` and `/readyz` can now be used as add-on liveness/readiness probes instead of scraping the full admin status payload.

### Changed

- Add-on startup wiring now syncs the runtime config path and reuses the owned go-librespot process when it already matches the current Supervisor config.
- Add-on documentation now mirrors the same onboarding steps and labels shown in the dashboard, so setup instructions do not drift between UI and docs.

### Fixed

- Add-on setup checks now resolve the default Apple Silicon Homebrew `go-librespot` path correctly when PATH is sparse.
- Spotify setup rechecks clear stale connection state and can use cached user auth when probing playlists, so the add-on reports `Demo Mode`, `Degraded`, and `Real Spotify Mode` more accurately.

## 1.1.3

### Fixed

- Conductor workspace setup now uses repo-owned lifecycle scripts instead of relying on an interactive shell snippet that could break before bootstrap starts.

## 1.1.1

- Initial Home Assistant add-on release
- One-click install with ingress (sidebar) support
- Automatic Home Assistant state integration via Supervisor API
- Configurable Anthropic API key, Spotify credentials, and station name
- Falls back gracefully without Spotify or Anthropic credentials
