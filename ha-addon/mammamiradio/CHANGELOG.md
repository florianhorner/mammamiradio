# Changelog

## 2.10.0

### Added
- Startup diagnostics: boot log shows config file path, cache dir, API key presence, and dependency status (ffmpeg/ytdlp).
- yt-dlp binary check: warns at boot when yt-dlp is enabled but not installed.

### Fixed
- **Critical**: HA addon config quoting bug caused `ANTHROPIC_API_KEY` to never be exported on every restart, silently falling back to OpenAI.
- Prewarm now runs in background — FastAPI is ready instantly (was blocking up to 20s).
- Normalization cache: skips FFmpeg re-encode for previously-processed tracks, saving 60+ seconds per restart on Pi hardware.
- Pre-normalize upcoming track before playback begins, eliminating queue starvation on Pi-class hardware.
- Operator stop (`/api/stop`) survives crash/restart/watchdog — no more unexpected auto-play after reboot.
- Triggers (banter, news flash, ad) were silently ignored when producer queue was full. Now handled immediately.
- Host cliché filter: 14 overused phrases now cause a retry.
- Engine Room HA context now shows in English.
- Ad format name and sound bed type now visible in dashboard during ad breaks.
- Dashboard shows "Preparing..." instead of raw type string when normalization fails.
- yt-dlp temp fragment dirs cleaned up after every download.
- Admin keyboard shortcuts removed (were firing while typing in search box).
- Fire-and-forget music persistence eliminates audible gaps between songs on Pi hardware.
- Status endpoint caching reduces I/O overhead from aggressive admin/dashboard polling.
- Download validation floor lowered so silence fallbacks aren't rejected. ffprobe timeout prevents thread starvation.
- Fallback clips no longer delete bundled demo assets after playback.

## 2.9.0

### Added
- Multi-session persona arcs: hosts warm up over sessions (stranger → acquaintance → friend → old_friend). Milestone sessions inject acknowledgment directives.
- Song cues: persistent per-track memory. Anthem detection (3+ plays, never skipped), skip-bit detection (2+ skips), LLM reaction cues. Displayed as TRACK MEMORY in banter prompts.
- Enhanced callbacks: structured format with song context alongside plain strings.
- Play history enrichment: skipped/listen_duration_s columns for cross-session anthem and skip-bit detection.
- Deeper HA context: 10 new entities, 4 new mood classifications, threshold reactive triggers, Casa dashboard card.
- Tiered HA prompt references with weather-mood fusion.

### Fixed
- Song cue youtube_id pinned to known track, preventing orphan rows from LLM hallucination.
- Cue text sanitized before re-injection into prompts (cross-session injection prevention).
- SQLite NULLS LAST replaced with portable CASE expression.
- Listener request button fixed (IIFE scoping bug).
- Clip rate limiter uses asyncio.Lock instead of threading.Lock.
- Song request keywords only activate when yt-dlp is enabled.

## 2.8.0

### Added
- 100-track catalog depth (up from 50). Local music/ blending with chart playlist.
- Host chemistry: differentiated energy instructions prevent both hosts sounding identical.
- Echo-style transitions: 20% of handovers mirror the song's fading energy.
- Banter depth: 4-6 exchanges (was 2-4), doubled token budget, dedup guard.

### Fixed
- /readyz always 503 when no listener connected — fixed with 30s startup window.
- audio_source stuck at "prewarm" in /healthz after startup.
- allow_ytdlp read twice from env; now uses config object as single source of truth.
- Clip rate-limit dict never pruned — now evicts entries older than 5 minutes.

## 2.7.0

### Added
- WTF clip sharing: capture last 30s of audio into a shareable MP3 clip.
- Studio bleed atmosphere and one-shot humanity events for live radio feel.
- 18 authentic Italian ad brands (Esselunga, Fiat, TIM, Barilla, etc.).
- Fast-talking pharma disclaimer at +90% TTS rate.
- Cache integrity check and boot summary log for operator diagnostics.
- Dashboard pipeline indicators, stop sticky state, and ad metadata display.
- Admin Engine Room tab with runtime stats and capabilities.
- Periodic chart refresh every 90 minutes mid-session.

### Fixed
- Move-to-next no longer destroys the pre-rendered queue.
- Song repetition fixed: charts now fetch 50 tracks (was 20).
- Up-next preview distinguishes rendered vs predicted segments.

### Changed
- SFX volume reduced ~12dB. Mid-bumpers play 25% of the time.

## 2.6.0

### Added

- Listeners can submit song requests and shoutouts from the dashboard or listener page.
- Requested songs are downloaded in the background and pinned to play next, with host announcement.
- Admin search now shows live web results alongside playlist matches. Queue a web result to download and play it immediately.
- Custom station name in the admin Radio tab, persisted across tabs via localStorage.

### Fixed

- Listener request fields are now sanitised before LLM interpolation (prompt injection protection).
- Background downloads are discarded if the playlist source changed while downloading.
- `/api/listener-request` now rejects non-string inputs with a 400 instead of crashing.
- `switch_playlist()` now also clears `force_next` to prevent bleed into new sources.
- Admin "↓ Queue" button always restores after download errors.
- Request form shows visible error feedback for all failure modes, not just rate-limit responses.
- `_download_ytdlp` uses the exact `youtube_id` URL when available instead of re-searching.

## 2.5.1

### Fixed

- Admin panel accessible from LAN without token (Tailscale, RFC1918 trusted).
- Credential status now shows "configured" indicator when keys are set via addon config.
- AI tier detection uses both Anthropic and OpenAI keys, not just Anthropic.
- First audio plays within seconds of connecting (pre-warmed at startup).
- Search field handles errors gracefully.

## 2.5.0

### Added
- Track rules system: flag a song mid-stream with a reaction; future banter references it
- Admin panel tab split: Music tab (queue/playlist) and Radio tab (hosts/pacing/logs)
- Flag Track button in Now Playing card

### Changed
- Crossfade Option B: music bed stays at 50% during host voice-over (was 30%)
- Station ID sting reduced to 15% volume — background texture, not jarring hit
- Host banter: more chaos, mid-conversation drops, abandoned sentences, absurdist tangents
- Transition lines: musical option added (~30% of transitions echo the song's vibe)

## 2.4.1

### Added

- Playlist search and filter restored in admin panel.
- Drag-and-drop playlist reorder with grip handles.

### Fixed

- Search endpoint now returns actual playlist results instead of empty array.
- Artist clustering prevention hardened (4-tier relaxation, tighter soft weights).
- Host personality descriptions synced to addon radio.toml.

## 2.4.0

### Added

- Volare Refined design system: dark espresso theme across listener and admin UIs.
- OpenAI API key now accepted as equivalent to Anthropic for AI tier detection.
- yt-dlp in health check panel (warns if missing, does not block startup).
- Reconnect silence fix: canned clip plays immediately when a listener reconnects after an idle period.
- Home context enrichment: event diffing, mood classification, weather arcs, and reactive impossible moments. The DJ now knows when you made coffee.
- Listener launch ceremony: animated pre-launch state with radio warming up.

### Fixed

- Ad double-bed artifact removed: ads now have one music bed, not two.
- Credential write now strips newlines to prevent env file injection.
- Hub close correctly resets listener count so producer idle gate works on restart.

---

## 2.3.1

### Added

- Artist diversity cap: no more than 2 tracks per artist from Apple Music charts.
- LRU cache eviction: oldest MP3s are deleted when cache exceeds 500 MB (configurable via MAMMAMIRADIO_MAX_CACHE_MB). Prevents SD card overflow on Raspberry Pi.
- `/api/status` now reports token cost estimate and cache disk usage.
- Listener gate: no API burn when nobody is listening.
- Ad sound beds: warm ambient sine bed under every ad voiceover.
- HA media_player entity: copy-paste YAML in DOCS.md for play/pause/skip with album art.
- Stable admin token: set once in HA UI, use in secrets.yaml for media_player integration.
- Station name on air: hosts say your station name naturally, matches station_name config.
- Sharper host personalities: Marco doubles down, Giulia cuts him off.

---

## 2.3.0

### Removed

- Spotify integration: no more go-librespot, Spotify Connect, or Spotipy OAuth.
- 3 addon config options removed: spotify_client_id, spotify_client_secret, playlist_spotify_url.
- `gcompat` package removed from Docker image (was for go-librespot binary).

### Changed

- 3-tier system: Demo Radio, Full AI Radio, Connected Home (was 5 Spotify-centric tiers).
- Startup timeout reduced from 300s to 120s (no go-librespot startup wait).
- Docker image is smaller and starts faster.
- Music comes from local files, yt-dlp chart downloads, and bundled demo tracks.

## 2.2.2

### Changed

- Listener page is now the default at `/`. Dashboard moved to `/dashboard`.
- Station name unified to "Mamma Mi Radio" across all files.
- `/readyz` returns HTTP 503 when station is starting (was 200).

### Fixed

- Song repetition after ~30 minutes on small playlists.
- "Move to upcoming" no longer confuses the diversity filter.
- go-librespot binary compatibility on Alpine (added gcompat).

### Added

- `MAMMAMIRADIO_SKIP_QUALITY_GATE=1` env var to bypass audio validation.
- Silence fallback duration increased from 5s to 35s+ to pass quality gate.

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
- CI: ruff noqa/lockfile fixes (pydantic-core reverted to 2.41.5 to match pydantic 2.12.5).

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
