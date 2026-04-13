# TODOs

## P2: Wire skip-bit detection into live reactive banter
The skip-bit cue is correctly created when a listener skips a track twice (`detect_skip_bit` in `streamer.py:667`), but the boolean return value is discarded. The AI host never has a "caught you skipping it again!" moment in the current banter cycle.

**Action:** In `_persist_skipped_music`, check the `detect_skip_bit` return value. When `True`, set `state.ha_pending_directive` with an Italian reactive line like "Florian ha saltato questa canzone per la Nth volta — reagisci in modo complice" so the next banter slot delivers the impossible moment live.

**Why it matters:** This is the structural moat use case — real radio cannot do this. The data collection is already working; the reactive hook is the missing one-liner.

**Effort:** S (CC: ~15 min) | **Files:** `mammamiradio/streamer.py:649`, `mammamiradio/models.py`
**Source:** /plan-eng-review, 2026-04-13

## ~~Music catalog depth — multi-source rotation~~ RESOLVED
- Charts raised to 100 tracks. Local `music/` MP3s auto-blended into chart playlist when `allow_ytdlp=true`. Covers 7h+ of unique content without repetition.
- **Completed:** v2.8.0 (2026-04-13)

## P2: Setup friction — still unresolved after 4 sessions
Every single live session has surfaced setup friction. Stream stability once running is excellent, but getting there takes effort. This is now a pattern, not a one-off.

**Observed across sessions:**
- Session 1: go-librespot chaos, missing API key
- Session 2: first user couldn't find stream URL
- Session 3: admin UI controls felt disconnected
- Session 4 (2026-04-12): "not super smooth as always" — even for Florian running his own stack

**Action:** Dedicated setup sprint. Goals:
- Zero-friction start from fresh install to first sound in <60 seconds
- Surface stream URL and key status immediately on first load
- Consider a "health check" endpoint that validates config before stream starts
- Possibly: streamline the HA addon config options (fewer required fields)

**Effort:** M (CC: ~1-2 hours) | **Files:** mammamiradio/main.py, mammamiradio/dashboard.html, ha-addon/mammamiradio/config.yaml
**Source:** Recurring across all 4 live sessions; confirmed pattern 2026-04-12

## P1: Product positioning decision
The product is stuck between "self-hosted household radio engine" and "consumer product with shareware upsell." Both CEO and Eng dual voices flagged this independently. Until this is decided, onboarding, monetization, and multi-tenancy work is built on sand.
**Action:** Interview 5 potential users (cafe owners, HA enthusiasts, music hobbyists). Decide: household engine or consumer product.
**Source:** /autoplan review, 2026-04-04

## P2: Competitive landscape document — ANSWERED by live session
Spotify AI DJ exists (launched 2023, expanding languages). No contingency plan if Spotify ships Italian DJ or expands the feature. ElevenLabs and Cartesia are commoditizing expressive TTS.
**Moat identified (2026-04-09):** Radio format absorbs AI imperfection as authenticity — Spotify optimizes for smooth/polished which is *wrong* for radio. Plus HA-context impossible moments Spotify structurally cannot replicate. Neither moat is copyable by a streaming platform.
**Action:** Write the 1-pager, but the answer is now clear. Frame it as: "Spotify can't be bad at radio. We can."
**Source:** /autoplan CEO dual voices, 2026-04-04 + 55min live session observation, 2026-04-09

## ~~P2: Invest in HA context as differentiator~~ RESOLVED
10 new entities (room lights, power sensors, star projectors, terrace lights). 4 new mood classifications. Tiered banter references (1 item or up to 2 when mood is active). Weather-mood fusion. Casa dashboard card. `ha_moments` API for public status, `ha_details` for admin. Numeric event passthrough fixed.
**Completed:** feat/deeper-ha-context (2026-04-13)

## P1: Casa card in listener.html (QA bug — deeper-ha-context)
Casa card was added to `dashboard.html` (served at `/dashboard`, admin auth) but public listeners hit `/` which serves `listener.html`. The card is invisible to actual listeners. Backend (`ha_moments` in `/public-status`) is correct — UI fix only.
**Fix:** Copy casa-card HTML (dashboard.html:703-707) + the `ha` block in `updateStatus()` into `listener.html` at equivalent positions.
**Effort:** XS (CC: ~10 min) | **Files:** mammamiradio/listener.html
**Source:** QA pass on feat/deeper-ha-context, 2026-04-13

## P2: Distribution strategy
No landing page, no hosted demo, no analytics, no invite loop. The product has no way to be discovered. PR readiness != adoption readiness.
**Action:** Create a landing page with embedded demo player. Add basic analytics (segment counts, session duration).
**Source:** /autoplan CEO review, 2026-04-04

## P2: Narrow add-on detection
Current state: `_is_addon()` in `mammamiradio/config.py` now uses `SUPERVISOR_TOKEN`/`HASSIO_TOKEN` as primary signals.

Where to start: add a regression test covering `/data/options.json` outside true add-on runtime to ensure non-add-on environments are never misdetected.

## P2: Add runtime startup diagnosis
Current state: docs explain local vs add-on paths, but the runtime does not report resolved config dir or active audio source at boot.

Why it matters: when startup breaks, operators still have to infer state from scattered logs instead of getting one clear answer.

Where to start: add a small diagnostic surface from the launcher or app startup that prints the resolved config dir, detected audio source (local/yt-dlp/charts), and any missing dependencies.

## ~~P2: Focus trap for setup gate modal overlay~~ RESOLVED
The setup gate overlay was removed in the v2.5.x refactor. The only remaining fixed overlay in dashboard.html is the transition notification (2s display, no interactive elements). No focus trap needed.
**Resolved:** 2026-04-12 — setup gate no longer exists as a blocking modal.

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

## P1: Live session feedback — 2026-04-09 (third session)

### ~~Transition sounds — HARSH~~ RESOLVED
- SFX volume reduced ~12dB, mid-bumpers play 25% of the time.
- **Completed:** v2.7.0 (2026-04-12)

### Song-to-host crossfade — explore "host sings along" technique
- Current fade at ~80% smoothness — Option A: improve the fade curve
- Option B (preferred): host picks up last lyric/melody moment and transitions out of it — "I sing along with it to phase out"
- Option B is transformative for immersion; jigginess between segments doesn't matter
- **Effort:** M-L | **Files:** mammamiradio/producer.py, mammamiradio/scriptwriter.py

### ~~Host chemistry — too controlled, missing energy~~ RESOLVED
- Differentiated energy instructions when both hosts are high-energy/chaotic: higher-energy host runs the chaos, lower-energy one cuts surgically. No more identical manic robots.
- **Completed:** v2.8.0 (2026-04-13)

### ~~Song cue + ruleset mechanism~~ RESOLVED
- Machine-derived per-track memory via `song_cues.py`. Anthem detection (3+ plays, never skipped), skip-bit detection (2+ skips), LLM reaction cues. Cues appear in banter prompts as TRACK MEMORY.
- **Completed:** v2.9.0 (2026-04-13)

### ~~Studio humanity events~~ RESOLVED
- One-shot cough/paper-rustle/chair-creak/pen-tap after 15+ segments. 4 SFX files generated.
- **Completed:** v2.7.0 (2026-04-12)

### Real radio advertisements — the anchor of reality (P2)
- Mix real movie/brand radio spots with AI-generated ones — real ads make AI ads feel realer by association
- Sources: movie studio press kits, official YouTube trailer audio via yt-dlp, Italian cinema press sites, AdForum
- Italian market radio spots for current releases (Disney, Warner, Universal all do Italian radio campaigns)
- Architecture: demo_assets/ads/real/ folder, producer randomly slots one real ad per N AI ads
- Legal: press kit material distributed for broadcast; non-commercial personal use is defensible
- **Effort:** S (code) + ongoing curation | **Files:** mammamiradio/producer.py, demo_assets/ads/real/

### Interviews as a segment type (P3 backlog)
- Host + guest format: scripted Q&A, phone-in style, recurring fictional guests or synthesized celebrity voices
- Natural escalation from current two-host banter
- **Effort:** L | **Files:** mammamiradio/scriptwriter.py, mammamiradio/producer.py

### ~~Ad brand palette — Italian authenticity~~ RESOLVED
- 18 real Italian brands across 7 categories. Seasonal rotation TBD (follow-up).
- **Completed:** v2.7.0 (2026-04-12)

### ~~BUG: "Move to upcoming" destroys the playlist~~ RESOLVED
- Removed queue purge from move_to_next. Pin takes effect after buffer drains naturally.
- **Completed:** v2.7.0 (2026-04-12)

### ~~BUG: Song repetition / playlist loses position~~ RESOLVED
- Charts limit raised from 20 to 50 tracks. Periodic 90-minute refresh merges new tracks.
- **Completed:** v2.7.0 (2026-04-12)

### ~~FEATURE: Fast-talking disclaimer for health/pharma ads~~ RESOLVED
- Pharma ads get disclaimer_goblin role at +90% TTS rate.
- **Completed:** v2.7.0 (2026-04-12)

### ~~"Studio bleed" atmosphere~~ RESOLVED
- Faint prior banter clips (-22dB) mixed under ~35% of music segments. Intentional and controllable.
- **Completed:** v2.7.0 (2026-04-12)

### ~~"Share WTF moment" — viral clip mechanism~~ RESOLVED
- Ring buffer + `POST /api/clip` + `GET /clips/{id}.mp3`. Clips auto-expire after 24h.
- Short URL generation and upload TBD (follow-up).
- **Completed:** v2.7.0 (2026-04-12)

### Italian pronunciation of English — PROTECT
- Love when Italian hosts mispronounce English song titles in Italian accent
- This is a charm point. Never remove it, amplify it.
- Check prompt engineering to ensure this is explicit behavior, not accidental

### Admin UI — three panels: Music / Radio / Engine Room
- Music Management: playlists, songs, queue, drag-drop
- Radio Simulation: hosts, scripts, banter, ad controls
- **Engine Room** (hidden/operator): logs, token consumption, API cost per session, model in use, queue depth, error rate, segment timing
- Token cost view doubles as shareware hook ("X Claude tokens this session")
- **Effort:** M | **Files:** mammamiradio/admin.html, mammamiradio/streamer.py (stats endpoint)

### Segment gap / dead air — weird pauses between sections
- Gaps between segments feel unnatural, break rhythm
- Separate issue from harsh SFX — this is silence/timing, not sound
- Likely: tail silence not stripped, segment handoff delay, or buffer underrun
- Investigate post-session: normalizer tail trim, producer queue timing
- **Effort:** S-M | **Files:** mammamiradio/normalizer.py, mammamiradio/producer.py

### Admin UI — split Music Management from Radio Simulation
- Currently blended but conceptually different surfaces:
  - "Music Management": playlists, songs, queue, drag-drop reorder
  - "Radio Simulation": host toggles, scripts, banter settings, ad controls
- Should be separate tabs or sections in admin
- **Effort:** M | **Files:** mammamiradio/admin.html

### Playlist drag-and-drop reorder (Admin)
- Standard UX: drag songs to reorder the queue
- Backend endpoints already exist (`/api/playlist/move`, `/api/playlist/move_to_next`)
- Just needs frontend drag-and-drop wired to those endpoints
- **Effort:** S | **Files:** mammamiradio/admin.html

---

## P1: Listener QA backlog (2026-04-09 live feedback)
- Re-enable direct playlist reordering UX in dashboard (backend endpoints already exist: `/api/playlist/move`, `/api/playlist/move_to_next`).
- Fix playlist source UX so URL import is explicit and does not conflict with search UI.
- Clarify playlist lifecycle in UI (what happens when station reaches end / how rotation works).
- Align "Up Next" preview with actual queued segments to avoid UI/audio desync.
- Add top-level pipeline indicators near "On Air" (Anthropic status, OpenAI fallback, degraded mode).
- Redesign Pacing UI for clarity (outcomes, cadence preview, plain-language effects).
- Add post-download/normalize tail-silence guard and skip-bad-track fallback for broken endings.
- Restore skeuomorphic radio visual language consistently across admin and listener.
- Keep manual `/api/stop` sticky until an explicit `/api/resume`; do not auto-resume stopped sessions after idle time.
- Make `scripts/stream_watch_server.py` work against secured stations by using an authenticated status path or a dedicated read-only endpoint instead of unauthenticated `/status` and `/api/capabilities`.

## P1: Make GHCR packages public (BLOCKER for HA addon install)
GHCR packages are private by default. HA Supervisor cannot pull private images — the addon install will fail silently with a confusing error if this is not done first.
**Action:** GitHub → Packages → mammamiradio-addon-amd64 → Change visibility → Public. Repeat for aarch64.
**Effort:** S (human: 2 min, no code) | **Priority:** P1 — must do before install attempt
**Source:** /plan-ceo-review + /plan-eng-review, 2026-04-11

## ~~P2: Cache integrity check on startup~~ RESOLVED
Purge cached files < 10KB on startup. Logs warning for each purged file.
**Completed:** v2.7.0 (2026-04-12)

## ~~P2: First-boot log summary line~~ RESOLVED
One-line boot summary at INFO level with config dir, audio source, API keys, HA, yt-dlp, track count.
**Completed:** v2.7.0 (2026-04-12)
