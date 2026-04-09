# Changelog

## 2.2.1

### Added

- Session stop persists across restarts: stopped state is saved to disk and restored on startup.
- Spotify transfer backoff: after 10 consecutive failures the transfer poller slows to ~5 min intervals, reducing log noise on add-on hardware without a Spotify device.
- Playlist index endpoints reject non-integer payloads without mutating state.

### Changed

- Silence removal appended to the normalize filter chain — reduces dead air between segments.
- Add-on detection no longer uses `/data/options.json` as a signal, preventing false add-on mode when that path is mounted in dev environments.
- SFX generation expressions simplified for FFmpeg compatibility.

### Fixed

- Stale chart-track ID test assertion.

## 2.2.0

### Added

- Audio quality gate: banter, ad, and music segments are validated before they reach the live queue. Corrupt yt-dlp downloads and silent placeholders are detected and retried automatically.
- `AudioToolError` separates ffprobe/ffmpeg binary failures from content rejects — the station keeps playing even if the validation tool is temporarily unavailable.
- Runtime health transparency in `/healthz`, `/readyz`, and `/status`: queue-shadow integrity, task liveness, playback epoch, and failover source visibility.
- Deterministic Up Next explainability with per-item `reason` text and explicit upcoming-source labels.
- Delivery guardrail hook `scripts/check-changelog-sync.sh` to block version bumps unless both changelogs are staged.

### Changed

- Quality gate validation now runs off the async event loop (executor), preventing stream freezes on lower-powered HA hardware during FFmpeg probing.
- Producer music sequencing is now deterministic by playlist order, keeping add-on playlist controls and Up Next aligned during long sessions.
- Runtime sync now trims stale shadow queue entries when drift is detected.
- Admin host personality sliders now ship with clearer axis language and quick presets for faster tuning.

## 2.1.0

### Added

- Impossible moments: time-aware, listener-aware banter that works without Spotify or Home Assistant
- New listener greeting: DJ acknowledges when someone tunes in
- Listener connection tracking on admin status API
- Compounding listener memory: returning listeners are recognized across sessions with personalized banter
- Track motif recording and session tracking for richer host callbacks
- Persona security: instruction-pattern filtering prevents stored prompt injection

## 2.0.2

### Added

- OpenAI fallback for AI-generated banter and ads (set `openai_api_key` in add-on config)
- Golden path onboarding guidance in dashboard and listener UI
- Interactive Spotify auth workaround for macOS `.local.local` mDNS bug

### Fixed

- FFmpeg `aevalsrc` crash (exit code 234) in bumper jingle generation
- Spotify OAuth callback URL mismatch with loopback canonicalization

## 2.0.1

### Fixed

- Add-on startup no longer exits immediately when `/data/options.json` is unreadable or invalid JSON. The error is logged and startup continues with defaults.
- Add-on startup no longer hard-fails when `/data` is not writable. It falls back to `/tmp/mammamiradio-data` so the web server can still start.
- Add-on runtime path overrides are now respected for cache/tmp/go-librespot config directories, avoiding hardcoded `/data` assumptions.
- `go-librespot` config sync now writes to the resolved runtime config directory.

## 2.0.0

Major release. The station now boots instantly with zero config and progressively unlocks capabilities.

### Breaking

- Capability flags replace the old mode system. Integrations reading mode must switch to `GET /api/capabilities`.

### Added

- **Demo-first boot**: station starts with zero config using built-in demo tracks and pre-bundled banter.
- **OpenAI TTS**: hosts can use `gpt-4o-mini-tts` via `engine = "openai"`. `OPENAI_API_KEY` supported in addon options.
- **Signature Ad System**: 6 formats, multi-voice casting, campaign memory, brand motifs, sonic signatures.
- **Spotify source picker**: choose playlists or Liked Songs. Persisted across restarts.
- **Personality sliders**: tune host energy, chaos, warmth, verbosity, and nostalgia.
- **Listener persona system**: tracks listening patterns and feeds them into banter prompts.
- **Dashboard Volare theme**: warm Italian sunset palette with capability-driven cards.

### Changed

- Bounded deques for automatic memory management.
- HA polling uses a reusable HTTP client singleton.
- Richer ad SFX, bumper jingles, and music beds with layered harmonics.
- Punchier ad processing (8:1 compression, presence boost, mud cut).
- FFmpeg: multi-input filter graphs collapsed into single expressions.
- Source switching triggers immediate cutover with queue purge.

### Fixed

- Status API deque serialization crash.
- Banter generation gracefully falls back without Anthropic key.
- Spotify playlist fetch zero-track bug.
- Producer recovery stall on go-librespot restart.

### Dependencies

- Docker GitHub Actions bumped to latest majors (setup-qemu 4.0, login 4.1, buildx 4.0, metadata 6.0, build-push 7.0).

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
