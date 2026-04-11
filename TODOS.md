# TODOs

## P1: Product positioning decision
The product is stuck between "self-hosted household radio engine" and "consumer product with shareware upsell." Both CEO and Eng dual voices flagged this independently. Until this is decided, onboarding, monetization, and multi-tenancy work is built on sand.
**Action:** Interview 5 potential users (cafe owners, HA enthusiasts, music hobbyists). Decide: household engine or consumer product.
**Source:** /autoplan review, 2026-04-04

## P2: Competitive landscape document — ANSWERED by live session
Spotify AI DJ exists (launched 2023, expanding languages). No contingency plan if Spotify ships Italian DJ or expands the feature. ElevenLabs and Cartesia are commoditizing expressive TTS.
**Moat identified (2026-04-09):** Radio format absorbs AI imperfection as authenticity — Spotify optimizes for smooth/polished which is *wrong* for radio. Plus HA-context impossible moments Spotify structurally cannot replicate. Neither moat is copyable by a streaming platform.
**Action:** Write the 1-pager, but the answer is now clear. Frame it as: "Spotify can't be bad at radio. We can."
**Source:** /autoplan CEO dual voices, 2026-04-04 + 55min live session observation, 2026-04-09

## P2: Invest in HA context as differentiator
All three review phases flagged: HA context is the only moat Spotify DJ cannot copy. The plan invests zero in it. Current HA integration (ha_context.py) is already structured but underutilized.
**Action:** Make HA context more visible in banter/ads. Add weather-aware, time-aware, and room-aware content prompts. Surface HA state in dashboard.
**Source:** /autoplan cross-phase theme, 2026-04-04

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

## P1: Live session feedback — 2026-04-09 (third session)

### Transition sounds — HARSH (fix before next session)
- The SFX/jingles between segments feel harsh and artificial
- Soften or remove entirely; silence or a very subtle breath is better than bad SFX
- **Files:** mammamiradio/normalizer.py, mammamiradio/producer.py

### Song-to-host crossfade — explore "host sings along" technique
- Current fade at ~80% smoothness — Option A: improve the fade curve
- Option B (preferred): host picks up last lyric/melody moment and transitions out of it — "I sing along with it to phase out"
- Option B is transformative for immersion; jigginess between segments doesn't matter
- **Effort:** M-L | **Files:** mammamiradio/producer.py, mammamiradio/scriptwriter.py

### Host chemistry — too controlled, missing energy
- Hosts sound too "unchaotic" relative to each other
- Missing: interruptions, strong reactions, real opinions, chaos of actual radio banter
- Need to feel like two people with real energy, not two robots taking turns
- **Effort:** M | **Files:** mammamiradio/scriptwriter.py, radio.toml

### Song cue + ruleset mechanism
- User needs to flag a specific song mid-stream → system accumulates per-track rules
- Example: Aggu Palermo cringe pop → host reaction "spot on cringe fest" was perfect — want to lock that in
- Build: highlight endpoint → rule stored → ruleset applied at next playback of flagged track
- This is different from persona memory — it's per-track annotation + reaction rules
- **Effort:** M | **Files:** mammamiradio/producer.py, mammamiradio/scriptwriter.py, new: mammamiradio/track_rules.py

### Studio humanity events — sparse, intentional one-shots (P1 impossible moment)
- Cough/sneeze mid-show (once per session max), paper rustling when host loses thought, door opening for news delivery
- These are EVENTS not ambient loops — scarcity is the mechanic. Once = magic. Ten times = annoying.
- Implementation: one-shot event scheduler in producer, SFX assets in demo_assets/sfx/studio/
- **Effort:** S+S | **Files:** mammamiradio/producer.py, demo_assets/sfx/studio/ (new)

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

### Ad brand palette — Italian authenticity (P2)
- Current ad generator uses generic placeholder brands; should use real Italian radio categories
- radio.toml ad_brands needs: supermarkets (Esselunga, Lidl, Conad), cars (Fiat, Alfa, Jeep), movies (current theatrical), telecom (TIM, Vodafone, WindTre), banking (Fineco, Poste), health (with fast disclaimer), food (Barilla, Ferrero), fashion (OVS, Zara seasonal)
- Seasonal rotation: Ferrero at Christmas, sunscreen in summer, school supplies in September
- **Files:** radio.toml, mammamiradio/scriptwriter.py

### BUG: "Move to upcoming" destroys the playlist (P1)
- Moving a track to "upcoming" via UI wiped the entire queue and triggered full rebuild
- Expected: reorder only, never destroy
- Investigate: does the endpoint purge state? is the frontend calling the wrong endpoint?
- **Files:** mammamiradio/admin.html, mammamiradio/streamer.py (playlist move endpoints)

### BUG: Song repetition / playlist loses position (P1)
- Songs repeat after ~30-40 min; feels like the 20-track Italian charts set loops from top
- Unclear if producer tracks cursor position across cycles or reshuffles each time
- "Move to upcoming" may reset the playlist cursor — double-whammy with the bug above
- **Files:** mammamiradio/producer.py, mammamiradio/playlist.py

### FEATURE: Fast-talking disclaimer for health/pharma ads (P2)
- Real radio health ads end with a legally-required disclaimer read at ~3-4x speed
- Currently read at normal pace — sounds wrong, breaks the illusion
- Implementation: scriptwriter marks disclaimer segment, TTS renders at elevated rate
- **Files:** mammamiradio/scriptwriter.py, mammamiradio/tts.py

### "Studio bleed" atmosphere — engineer the accidental Italian chatter (P1 impossible moment)
- At 32min mark, user heard Italian voices in background during transition — sounded like someone left a mic on
- Not confused in a bad way — "borderline confused in a good way. uncanny."
- Likely: banter TTS clips bleeding through at low volume during segment handoff
- Action: find exact mechanism, then make it intentional and controllable
- Design: persistent low-volume Italian studio ambient layer under all transitions — NOT silence, not SFX, but the sense that the studio is always live
- This is what separates a playlist with voiceover from a radio station with a soul
- Do NOT remove or "fix" this behavior before understanding it
- **Effort:** S (understand) + M (engineer intentionally) | **Files:** mammamiradio/producer.py, mammamiradio/normalizer.py

### "Share WTF moment" — viral clip mechanism (P1 product idea)
- Listener presses a button → last ~30s trimmed into a clip → short URL to share
- Already felt the need 2-3 times in this single session — strong signal
- The clip IS the marketing. Nobody explains AI radio, they send a clip.
- Retroactive: "replay last 30s" → decide to share
- Pairs with song cue mechanism: flagging a moment = potential WTF clip candidate
- Architecture: rolling ring buffer of raw stream audio → clip on demand → upload → short URL
- **Effort:** M | **Files:** mammamiradio/streamer.py (ring buffer), new: mammamiradio/clip.py

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
