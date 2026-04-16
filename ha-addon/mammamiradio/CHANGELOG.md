# Changelog

## 2.10.5

### Changed

- Admin UI redesign: two-column control room layout with warm sidebar, compact now-playing card, waveform/progress, 2×2 quick-controls grid (Next / Pause / Shuffle / Banter), unified "On Air" programme list with NOW badge, and filter pills (All / Music / Banter / Ads). Pacing, Hosts, Station Log, Engine Room collapsed into accordions.
- Token cost counter regression fix: static element no longer shadows the dynamic Engine Room cost display.
- Stop/Resume 2×2 grid fix: no more visual gap when toggling Stop↔Resume.
- Accessibility polish: keyboard `:focus-visible` ring on buttons/inputs, 44px touch-target floor on controls (36px chips, 32px pills), base font-size raised to 16px (WCAG), queue title raised to 14px, HA slider labels raised to 9px/32% opacity.
- Quick Action labels renamed to action-oriented verbs (trim / force).
- Dead `btn-skip` CSS removed; hardcoded hover hex replaced with `color-mix` on the accent token.

## 2.10.4

### Security

- CI action SHA-pinned: `dependabot/fetch-metadata` now pinned to commit SHA (supply chain hardening).
- Added `.gitleaks.toml` for secret scanning (Anthropic API keys, HA tokens).
- Raised `yt-dlp` minimum version to `>=2026.2.21` (patches GHSA-g3gw-q23r-pgqm).

## 2.10.3

### Added
- `POST /api/hot-reload`: reloads `mammamiradio.scriptwriter` in place without interrupting the stream.
- Quick Actions chips in admin UI: one-tap controls for Less banter / More chaos / Too many ads / Hot reload.

### Changed
- Producer now imports `mammamiradio.scriptwriter` as a module reference so hot reload applies at every call site.
- `_has_script_llm` renamed to `has_script_llm` for the new module-reference import pattern.
- HA add-on `radio.toml` now ships byte-for-byte identical to the root `radio.toml`. The Pi-specific pacing overrides (`songs_between_banter=3`, `ad_spots_per_break=1`, `lookahead_segments=2`) are removed; CI, the local validator, and `tests/test_addon_radio_sync.py` all enforce strict `cmp -s`.
- Broadcast EQ restored to the 3-filter chain.
- Auto-resume on listener connect removed. A deliberate `/api/stop` stays paused across restarts until explicit `/api/resume`.

## 2.10.2

### Fixed
- **Critical**: Silence after HA restart following a deliberate stop. The `session_stopped.flag` survived restarts — any listener connecting after a restart heard nothing until a manual admin resume. Fixed: listener connecting now auto-clears the stopped state.
- **Critical**: 55-75 second silence on resume and idle wakeup on Pi. No canned banter clips ship in the container, so the bridge had no audio to play. Both resume and idle bridges now fall back to the first pre-normalized track in cache, available immediately without FFmpeg.
- **Critical**: FFmpeg 8.1 SIGABRT during normalization on Pi aarch64. Three equalizer filters + loudnorm trigger a `calc_energy` assertion crash (`psymodel.c:576`). Third equalizer removed. Every track was silently failing to normalize, leaving the queue permanently empty.
- Stream player no longer requires a page reload after admin resume. Auto-reconnects within 300ms of the status flip.

## 2.10.1

### Fixed
- **Critical**: Docker images for 2.10.0 were never built. The CI validate job used a strict byte-comparison of `radio.toml` files, but the HA add-on intentionally carries Pi/HA Green pacing overrides. Validate always failed, blocking image builds — HA Supervisor got `[404] manifest unknown` on every update attempt.
- **Pi pacing tuning discarded**: The build step was copying the root `radio.toml` (higher CPU load defaults) over the HA-specific one, shipping the wrong pacing values baked into the image.
