# TODOs

## P1: Product positioning decision
The product is stuck between "self-hosted household radio engine" and "consumer product with shareware upsell." Both CEO and Eng dual voices flagged this independently. Until this is decided, onboarding, monetization, and multi-tenancy work is built on sand.
**Action:** Interview 5 potential users (cafe owners, HA enthusiasts, music hobbyists). Decide: household engine or consumer product.
**Source:** /autoplan review, 2026-04-04

## P2: Competitive landscape document
Spotify AI DJ exists (launched 2023, expanding languages). No contingency plan if Spotify ships Italian DJ or expands the feature. ElevenLabs and Cartesia are commoditizing expressive TTS.
**Action:** Write a 1-page competitive landscape. Define the moat (HA context? Italian authenticity? Self-hosted?). Set a trigger: "If Spotify launches X, we pivot to Y."
**Source:** /autoplan CEO dual voices, 2026-04-04

## P2: Invest in HA context as differentiator
All three review phases flagged: HA context is the only moat Spotify DJ cannot copy. The plan invests zero in it. Current HA integration (ha_context.py) is already structured but underutilized.
**Action:** Make HA context more visible in banter/ads. Add weather-aware, time-aware, and room-aware content prompts. Surface HA state in dashboard.
**Source:** /autoplan cross-phase theme, 2026-04-04

## P2: Distribution strategy
No landing page, no hosted demo, no analytics, no invite loop. The product has no way to be discovered. PR readiness != adoption readiness.
**Action:** Create a landing page with embedded demo player. Add basic analytics (segment counts, session duration). Consider Spotify embed integration.
**Source:** /autoplan CEO review, 2026-04-04

## P2: Narrow add-on detection
Current state: `_is_addon()` in `mammamiradio/config.py` treats `/data/options.json` as sufficient proof of add-on mode.

Why it matters: that can coerce a non-add-on environment onto `/data/go-librespot`, which recreates the same path-confusion class in a different form.

Where to start: tighten add-on detection so token-based Supervisor signals are primary, then add a regression test covering `/data/options.json` outside true add-on runtime.

## P2: Add runtime startup diagnosis
Current state: docs explain local vs add-on paths, but the runtime does not report resolved config dir, ownership mode, or why it attached vs spawned `go-librespot`.

Why it matters: when startup breaks, operators still have to infer state from scattered logs instead of getting one clear answer.

Where to start: add a small diagnostic surface from the launcher or app startup that prints the resolved config dir, active `go-librespot` PID ownership, and mismatch reasons.

## P2: Focus trap for setup gate modal overlay
The setup gate overlay does not trap keyboard focus. Tab key can reach elements behind the overlay, which breaks accessibility for keyboard and screen reader users. Standard modal pattern: trap focus inside the overlay while open, restore on close.
**Effort:** S (CC: ~5min) | **Depends on:** nothing | **Files:** mammamiradio/dashboard.html

## P3: Extract setup gate UI from dashboard.html
dashboard.html is now 1600+ lines with ~620 lines of inline setup gate CSS/JS/HTML. Extract into a separate template or at minimum a JS module. The file is the most-modified in the repo (28 touches in 30 days) and the monolith makes merge conflicts more likely.
**Effort:** S (CC: ~15min) | **Depends on:** nothing | **Files:** mammamiradio/dashboard.html, mammamiradio/streamer.py

## P3: Pre-recorded SFX asset pack
The signature ad system uses synthetic ffmpeg sine waves for all SFX and environment beds. The sfx_dir mechanism already supports pre-recorded files (checked first before synthetic fallback). A curated pack of 10-15 real SFX files (cash register, cafe ambience, beach waves, mandolin sting, etc.) would make the biggest single-item audio quality improvement with zero code changes.
**Effort:** S (CC: n/a, manual curation) | **Depends on:** signature ad system (defines SFX type names) | **Files:** sfx/

## P3: Dashboard ad format display
The signature ad system adds format, sonic world, and speaker role metadata to state.last_ad_script. The dashboard at / reads this via /status but doesn't render the new fields yet. Show ad format, sonic palette, and cast info in the ad break section.
**Effort:** S (CC: ~10min) | **Depends on:** signature ad system | **Files:** mammamiradio/dashboard.html

## P3: LLM eval suite for ad format compliance
The signature ad system provides rich format instructions (duo_scene should produce 2 roles, late_night_whisper should use slow pacing, etc.) but no automated way to verify LLM output follows them. An eval suite with golden examples and scoring rubrics would catch prompt regressions when the ad prompt or Claude model changes.
**Effort:** M (CC: ~2-3 hours) | **Depends on:** signature ad system | **Files:** tests/eval_ads.py (new)

## P3: Wire disclaimer_goblin into format system
The disclaimer_goblin role is defined in SPEAKER_ROLES and has a voice in radio.toml (Rinaldo), but no format in _FORMAT_ROLES ever requests it. It can only appear via random fallback casting. Consider adding it as a secondary role for classic_pitch or testimonial formats, or creating a new format that features it.
**Effort:** S (CC: ~5min) | **Depends on:** signature ad system | **Files:** mammamiradio/producer.py

## P1: Listener QA backlog (2026-04-09 live feedback)
- Re-enable direct playlist reordering UX in dashboard (backend endpoints already exist: `/api/playlist/move`, `/api/playlist/move_to_next`).
- Fix playlist source UX so Spotify URL import is explicit and does not conflict with search UI.
- Clarify playlist lifecycle in UI (what happens when station reaches end / how rotation works).
- Align "Up Next" preview with actual queued segments to avoid UI/audio desync.
- Add top-level pipeline indicators near "On Air" (Anthropic status, OpenAI fallback, degraded mode).
- Redesign Pacing UI for clarity (outcomes, cadence preview, plain-language effects).
- Add post-download/normalize tail-silence guard and skip-bad-track fallback for broken endings.
- Restore skeuomorphic radio visual language consistently across admin and listener.
- Keep manual `/api/stop` sticky until an explicit `/api/resume`; do not auto-resume stopped sessions after idle time.
- Make `scripts/stream_watch_server.py` work against secured stations by using an authenticated status path or a dedicated read-only endpoint instead of unauthenticated `/status` and `/api/capabilities`.
