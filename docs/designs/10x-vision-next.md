<!-- /autoplan restore point: ~/.gstack/projects/florianhorner-mammamiradio/florianhorner-feat-10x-vision-autoplan-restore-20260404-232448.md -->
# 10x Vision: Next Steps

Status: ACTIVE
Branch: florianhorner/feat-10x-vision
Last session: 2026-04-04
Author: Wellington workspace

## Context

The 10x vision prototype is working. Autoplay + personalized banter delivered goosebumps
in the first live test. The FM dial UI, waveform visualizer, scrolling ticker, and station
language are in place. 432 tests pass, full code review complete, all findings fixed.

Three categories of work remain: stability fixes (blocking testing), shipping blockers
(blocking PR merge), and experience upgrades (next iteration).

---

## P0: Stability Fixes (block testing)

### 1. FIFO drain backoff

**Problem:** When go-librespot disconnects or the FIFO has no writer, `_drain_fifo()`
in `spotify_player.py` spins on `EAGAIN` (errno 35) with no backoff. Hundreds of
warnings per second, CPU spike, log flood.

**File:** `mammamiradio/spotify_player.py`, `_drain_fifo()` method, the `except OSError`
handler around line 144.

**Fix:** Add `time.sleep(0.5)` in the error handler before reopening. The drain thread
already has a 1-second select timeout for graceful shutdown. The backoff just prevents
the tight reopen loop.

**Effort:** S (5 min)

### 2. Port stability on restart

**Problem:** Conductor assigns dynamic ports (55000, 55010, etc.) between workspace
restarts. The go-librespot config gets patched via `sed` in `start.sh`, but stale
Python processes from previous runs hold old ports. `Address already in use` on
~30% of restarts.

**File:** `start.sh`, startup sequence before uvicorn launch.

**Fix:** Add a port-free check before launching uvicorn. If the port is held, log
which PID holds it and exit with a clear error instead of silently failing. Optionally:
`lsof -ti :$PORT | xargs kill` with a warning.

**Effort:** S (15 min)

### 3. TTS error resilience

**Problem:** `GianniNeural` (Marco's voice) fails in bursts on edge-tts. When it fails,
`synthesize()` falls back to generating silence, which produces dead air in the stream.

**File:** `mammamiradio/tts.py`, `synthesize()` function.

**Fix:** On TTS failure, try a fallback voice (DiegoNeural) before resorting to silence.
Add a `fallback_voice` parameter or a hardcoded retry with the alternate voice.

**Effort:** S (15 min)

---

## P1: Shipping Blockers (block PR merge)

### 4. Royalty-free demo music

**Problem:** Demo tracks are downloaded via yt-dlp from YouTube. Copyright violation.
Cannot ship this as the default path.

**Current state:** `mammamiradio/downloader.py` falls through to yt-dlp for demo tracks
(spotify_id starts with "demo"). The `demo_assets/music/` directory exists but is empty.

**Options:**
- A) Source 5-7 CC0/CC-BY Italian-flavored tracks (mandolin, accordion, Italian vocals).
  Bundle as MP3s in `demo_assets/music/`. The downloader already checks this directory
  first via `_find_demo_asset()`.
- B) Generate synthetic Italian music beds using the FFmpeg pipeline in `normalizer.py`.
  Lower quality but zero licensing risk.
- C) Keep yt-dlp as an opt-in flag (`MAMMAMIRADIO_ALLOW_YTDLP=true`) but default to
  bundled assets only. Best of both worlds for dev vs shipping.

**Recommendation:** C for now (unblocks shipping), then A when tracks are sourced.

**Effort:** M (C alone is S, sourcing CC tracks for A takes time)

### 5. Commit and clean the working tree

**Problem:** The branch has 17 commits from the designer and this session. Some are
iterative ("design: prototype dial card layout", "design: remove fake FM frequency").
The working tree may have uncommitted changes from the last round of fixes.

**Fix:** Stage all changes, commit with a clear message. Consider squashing the design
iteration commits into one before PR. Run `make check` (lint + typecheck + test) as
final gate.

**Effort:** S (15 min)

---

## P2: Experience Upgrades (next iteration)

### 6. Radio dial animation polish

**Problem:** The dial needle animation is smoother than before but still not perfect.
The collapse transition works but could feel more organic. The user wants "alive, not
technical."

**Files:** `mammamiradio/dashboard.html`, dial CSS and JS sections.

**Work:**
- Longer seek phase (10-12s before lock-in, currently 8s)
- More frequency band coverage during search (currently 88-107.5, use full 87.5-108)
- Smoother collapse: consider crossfading to a mini frequency display instead of
  scaling to zero
- The crackling audio needs testing across browsers (Web Audio API compatibility)

**Effort:** M (design iteration, hard to scope)

### 7. Scrolling track name (marquee)

**Problem:** Long track names overflow the now-playing card. Real radio displays scroll
the text.

**File:** `mammamiradio/dashboard.html`, `.track` CSS class and `updateStatus()` JS.

**Fix:** CSS `overflow: hidden` + `@keyframes` horizontal scroll when text overflows
container width. Only animate when overflow is detected (JS measures `scrollWidth` vs
`clientWidth`). Stop animation when track changes (reset position).

**Effort:** S (30 min)

### 8. Welcome banter on Spotify connect

**Problem:** When Spotify connects, the hosts should audibly react. Currently the
transition is visual only (dial shift + "Benvenuto" overlay). The golden path says:
"The DJ literally interrupts the broadcast."

**Implementation:**
- Pre-generate 3-4 welcome clips via edge-tts: Marco says "Eyyy, qualcuno si e
  collegato!" / Giulia says "Benvenuto, vediamo cosa ci hai portato..."
- Store in `demo_assets/welcome/`
- In the autoplay block in `producer.py`, queue a welcome clip BEFORE the captured
  song (currently it goes: [captured song] -> [banter about song])
- New order: [welcome clip] -> [captured song] -> [banter about song]

**Effort:** S (30 min, assuming edge-tts cooperates)

### 9. F5-TTS prototype

**Problem:** Edge-tts voices are flat and robotic. The "Full AI Radio" tier deserves
expressive voices. F5-TTS does zero-shot voice cloning from a reference clip.

**Work:**
- Create a branch `florianhorner/feat-f5tts-prototype`
- Install F5-TTS, verify it runs on Apple Silicon MPS
- Record or source 10-15s reference clips for Marco (enthusiastic) and Giulia (deadpan)
- Add `f5tts` backend to `synthesize()` in `tts.py`
- A/B test: generate the same banter script with edge-tts and F5-TTS, compare
- Measure inference latency (target: <3s with queue lookahead)

**Effort:** L (research + validation + integration)

**See also:** `docs/designs/tts-architecture.md` for the full TTS strategy.

### 10. Shareware trial enforcement

**Problem:** The first 2-3 banter clips should be high-quality gold clips, then
quality drops to TTS fallback, prompting the user to add their Anthropic key.

**Current state:** `_pick_canned_clip()` rotates through 15 clips with anti-repeat.
But there's no counter — all 15 play before any repeat. There's no quality drop
signal to the dashboard.

**Fix:**
- Add a `canned_clips_played` counter to `StationState`
- After 3 canned clips, stop picking them (force TTS fallback)
- Send the counter in `/api/capabilities` so the dashboard can show "Bring the
  hosts to life" at the right moment
- The quality drop IS the sales pitch — don't try to hide it

**Effort:** S (30 min)

---

## Sequence

```
P0 (today/tomorrow):
  1. FIFO backoff ──┐
  2. Port stability ├── unblocks stable testing
  3. TTS fallback ──┘

P1 (before PR):
  4. Royalty-free flag (option C) ── unblocks shipping
  5. Commit + clean ── PR ready

P2 (next session):
  7. Marquee track name ──┐
  8. Welcome banter clips ├── experience polish
  10. Shareware trial ────┘
  6. Dial animation ── design iteration
  9. F5-TTS prototype ── separate branch
```

## Files touched by this plan

```
mammamiradio/spotify_player.py    — #1 FIFO backoff
start.sh                          — #2 port check
mammamiradio/tts.py               — #3 TTS fallback voice
mammamiradio/downloader.py        — #4 yt-dlp opt-in flag
mammamiradio/dashboard.html       — #6 dial, #7 marquee
mammamiradio/producer.py          — #8 welcome clip, #10 shareware counter
mammamiradio/models.py            — #10 canned_clips_played counter
demo_assets/welcome/              — #8 welcome clips (new directory)
```
