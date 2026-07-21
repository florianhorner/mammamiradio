# Architecture

`mammamiradio` is one FastAPI process with one shared station timeline in memory.

One background task stays ahead and produces segments. Another reads the next ready segment and streams it to every connected listener at real playback speed.

## Runtime overview

```text
Charts / Jamendo / classic eras / local files / demo tracks
                |
                v
           playlist.py
                |
                v
          StationState + scheduler.py
                |
                v
        producer.py renders Segment files
                |
                v
          asyncio.Queue[Segment]
                |
                v
   streamer.py playback loop -> LiveStreamHub -> /stream and /listen
                |                    |
                |                    +-> aggregate active count
                |                              |
                |                              v
                |                       ListenerSession
                |                    (in-memory station epoch)
                |                         |             |
                |          async receipt  |             +-> one cue after
                |                         v                 30 active minutes
                |                  PersonaStore                 |
                |                    (SQLite)                   v
                |                                  producer.py atomic claim
                |
                +-> /public-status (public contract, no session diagnostics)
                +-> /status (admin-only anonymous session diagnostics)
```

## Startup flow

`mammamiradio.main:startup()` does ten things:

1. Loads `radio.toml` and `.env` through `config.py`.
2. Validates the config and applies legacy migration like `station.bitrate -> audio.bitrate`.
3. Purges suspect cache files (< 10KB, likely failed downloads) and evicts old cache entries.
4. Captures the install-scoped Home context boundary before SQLite initialization, then cross-checks its sidecar witness with a redundant DB-local witness after initialization. Missing, corrupt, or disagreeing R0 witnesses fail narrow; a cold install can therefore never become legacy merely because its database exists on a later boot.
5. Restores persisted source selection from `cache/playlist_source.json`, then fetches the playlist by walking the priority chain (charts → Jamendo → local `music/` → bundled demo assets → built-in `DEMO_TRACKS`) and falling through to the next source whenever a tier is gated off, unconfigured, or empty.
6. Initializes the clip ring buffer for WTF clip sharing.
7. Restores persisted `chaos_mode_active` from `MAMMAMIRADIO_CHAOS_MODE` or HA add-on `/data/options.json` without arming a first strike.
8. Creates shared app state, then synchronously admits any safe `cache/restart_handoff/` music segments straight into the queue (see "Restart handoff spool" below) — before the background producer/playback tasks start, so a listener connecting right after an update can reach an already-normalized track instead of an empty queue.
9. Launches:
   - `run_producer()` to fill the lookahead queue
   - `run_playback_loop()` to stream queued audio
10. Logs a one-line boot summary with resolved config dir, audio source, API key presence, HA status, and track count.

### Restart handoff spool

`mammamiradio/restart_handoff.py` owns a small durable spool the producer writes to and startup reads from, purely to shorten the gap between an add-on update finishing and the first listener hearing live programming again:

- After each music segment is queued, the producer (`scheduling/producer.py::_schedule_restart_handoff_spool`) best-effort copies it (hash-addressed, content-verified) into `cache/restart_handoff/segments/` and atomically publishes a small `manifest.json` describing up to `DEFAULT_MAX_ENTRIES` (3) recent, already-normalized, non-ephemeral music tracks. Older/unreferenced spool files are pruned on each write; files still queued for playback this session are protected from that prune.
- On the next boot, `main.py::_admit_restart_handoff` loads and validates the manifest (`admit_restart_handoff_entries`) — checking file existence, size, SHA-256, age (`DEFAULT_MAX_ENTRY_AGE_SEC`, 6h), and the operator blocklist — and enqueues whatever passes validation before the producer or playback loop has started. A corrupt, stale, or missing manifest is a silent no-op; the normal cold-start rescue ladder (see `docs/operations.md`) still applies underneath it.
- Skipped entirely when `session_stopped` is set (the station was deliberately stopped, not just updated) so a stopped station doesn't quietly start playing again.
- This is independent of, and does not replace, the norm-cache/demo-asset rescue ladder described in `docs/operations.md` — it is a *faster* first source when it has something to offer, not a new failure mode when it doesn't.
- A hard kill between a spool write's `mkstemp` and its final `os.replace` can leave an orphaned `.manifest-*.tmp` (in `cache/restart_handoff/`) or `.handoff-*.tmp` (in `cache/restart_handoff/segments/`) scratch file behind. `prune_stale_handoff_tmp_files` sweeps both directories at the start of every boot (before `_admit_restart_handoff` runs), deleting only scratch files older than 6h, with path-containment and symlink checks so a corrupted/symlinked cache dir degrades to a no-op rather than raising, and a per-directory cap (500 candidates, oldest first) so a pathological backlog can't stall startup. A second, independent ceiling (5000 candidates) bounds the raw `glob()` enumeration itself, so even an extreme backlog can't make the scan/sort step unbounded before the 500-candidate prune cap gets a chance to apply.

### Release beat campaign

`mammamiradio/release_campaign.py` turns an optional packaged `mammamiradio/assets/release/release_beat.toml` manifest into a bounded, listener-safe on-air announcement after an update:

- The manifest ships disabled/absent by default — no file, or `enabled = false`, means the feature is a complete no-op and nothing changes. `scripts/validate-release-beat.py` validates its schema and listener-safe copy in CI (see `docs/runbooks/ha-addon.md` → "Release invariants gate").
- When enabled, `ReleaseCampaign` (loaded once at startup, persisted to `cache/release_campaign_ledger.json`) offers the scriptwriter a release-beat prompt block on the first eligible banter break; `hosts/scriptwriter.py` decides whether it actually made it into the spoken lines (`release_beat_used`).
- Delivery is only counted once the segment actually airs to a real, connected listener (`_emit_release_campaign_result` in `web/streamer.py`, reading the same Tier-3 stream-result hook the provenance ledger uses) — a queued-but-discarded or skipped segment does not spend one of the campaign's `max_airings`.
- The campaign self-retires on its own budget (`max_airings`, default 5) or time window (`campaign_window_seconds`, default 72h), independent of whether Show Memory (the provenance ledger) is enabled.

### Heading overlay

The admin Rotazione tab can steer the next stretch of music without replacing the
base playlist:

- `POST /api/heading {"seed": "classic://italian/80s"}` loads one of the existing
  classic Italian era sources, filters the operator blocklist, dedupes against the
  live pool, tags newly blended tracks with the active `Heading.id`, and bumps
  `playlist_revision` once. A zero-result import returns warm operator copy and
  does not arm narration.
- `POST /api/direction {"text": "2000s female vocals"}` (also accepted as
  `/api/heading` with `text`) expands operator text into concrete `{artist,title}`
  targets, searches yt-dlp metadata, and starts audio downloads in background via
  the same `_commit_external_download` boundary used by listener/admin external
  songs. It never pins, purges, or blocks the live queue. Existing matching tracks
  are retagged immediately; resolved new targets join rotation only if the source
  revision and active heading still match when the download finishes.
- `POST /api/heading/clear` is manual Back to auto. It clears `StationState.heading`
  and deletes `cache/heading.json`; already blended tracks remain in rotation and
  age out naturally. There is no purge and no audio interruption.
- `/api/playlist/load` is a true source replacement and clears the active heading
  plus `heading.json`, so a restart cannot reapply an old course over a freshly
  loaded base.

`cache/heading.json` is an overlay, separate from `playlist_source.json`. Reads are
corrupt/missing tolerant and return no heading rather than failing boot. Seed
headings persist the seed; text directions persist concrete targets, phase, safe
counts, and Record Hunt narration throttle fields. Restore splits by kind to honor
INSTANT AUDIO: a **seed** heading still restores synchronously during startup
(re-fetch the source, re-tag matching tracks, blend new ones at the back of the
rotation; on empty/failure it deletes `heading.json` and continues in auto). A
**text direction** does NOT re-search yt-dlp on the boot path — startup re-tags any
target already present in the freshly-fetched pool and marks the course active
immediately, then defers the network target re-search + downloads to a background
task (`_restore_direction_targets_background`) dispatched *after* the
producer/playback tasks start, so a slow search can never delay first audio. Until
that background resolve lands its first track, the course reports `phase:
"hunting"` / `resolving: true` (the admin banner shows the station hunting
records); if the background resolve yields no playable track, it clears the heading
back to auto and deletes `heading.json`. Persisted-heading writes (phase changes,
safe count updates, and narration throttle changes) are serialized under
`source_switch_lock` with a fresh identity re-check, so a write racing a "Back to
auto" can't resurrect a just-cleared course on the next restart.

Narration and stickiness are selection-driven, not queue-control-driven.
`StationState.select_next_track()` first applies the normal diversity filters and
then gives eligible tracks tagged with the active heading id an **adaptive lift**:
the multiplier is sized from the live pool so the hunt set reliably lands roughly
`HEADING_TARGET_SHARE` of picks no matter how large rotation is (a fixed ×N is
inaudible in a 200-track pool), clamped to `[HEADING_MIN_LIFT, HEADING_MAX_LIFT]`
so a small pool keeps the historical ×4 floor and a tiny hunt set can never make
one song dominate. Cooldowns, bans, artist diversity, pinned tracks, and rescue
paths can still win; the heading never purges the queue, forces play-next, or
interrupts audio.
`heading_pending_announcement` is armed for hunt start, first found record, and
occasional crate-digging beats. The next ordinary host break consumes that
dedicated slot at prompt-build into a Record Hunt block; it does not reuse or
overwrite `ha_pending_directive`, and it waits behind listener requests, HA
directives, chaos interrupts, release beats, festival beats, and new-listener
moments. Because the line asserts crate-digging momentum, not exact playlist state,
it is intentionally allowed to air even if Back to auto or another heading lands
while the banter is rendering. Consuming the notice persists the relevant narration
flag/counter best-effort so restarts do not redundantly re-notice. The music turn
always remains best-effort and never blocks or delays audio.

## Segment production

`scheduler.py` is the single source of truth for pacing:

- the first segment is always music
- ad breaks trigger when `songs_since_ad >= songs_between_ads`
- banter triggers when `songs_since_banter` crosses the configured threshold, with a small random jitter outside preview mode
- after a natural pacing decision, `producer.py` applies a runway governor to optional speech (`BANTER`, `AD`, `NEWS_FLASH`, `STATION_ID`, `TIME_CHECK`): if the real queued audio is below 240 seconds and the bounded queue can still build more runway, that pick becomes `MUSIC`; if the queue is effectively saturated below the floor, the due speech is allowed; operator forces, chaos first-strike, release-campaign forced banter, bridges, and error recovery stay outside that gate

`producer.py` turns that pacing decision into actual audio files:

- `MUSIC`
  - uses local `music/` files, then `yt-dlp` for chart tracks; when all candidates fail the audio quality gate, recycles the last-known-good music norm file, then drops the track and lets the playback rescue path handle the gap — silent audio is never queued
  - normalizes output before queueing
  - after each music segment lands, launches a background prefetch that normalizes the predicted next track so it's already cached by the time the current one finishes (~3-4 min), avoiding the 75s Pi stall on queue drain. A running prefetch is left to finish rather than cancelled and replaced — cancelling can't stop its in-flight executor FFmpeg, which would keep holding the background admission slot (see [Egress FX pipeline](#egress-fx-pipeline-the-transmitter-applied-last)) while a replacement parks another thread behind it; the next music segment just retries with a fresh candidate
- `BANTER`
  - asks Claude (or OpenAI as fallback) for structured dialogue JSON
  - synthesizes one line per host via the configured TTS engine (see [TTS architecture](#tts-architecture) below)
  - passes generated host speech through the imaging layer so banter and news can sit over a quiet music bed, falling back to a synthetic pad on cold starts
  - preserves running jokes in `StationState`
  - snapshots the generated evidence needed for station/song memory, but persists it only after the final aired banter script has streamed cleanly
  - when Chaos Mode is active, applies the per-call `CHAOS_MODE_BLOCK` and one `ChaosSubtype` prompt fragment while keeping the segment type as `BANTER`
- `AD`
  - picks brands with recurrence weighting and recent-brand avoidance
  - selects one of 6 ad formats: classic pitch, testimonial, duo scene, live remote, late-night whisper, or institutional PSA
  - resolves a sonic world (SFX, music bed mood, environment bed) per brand category
  - casts speakers by role — duo scenes and testimonials use two distinct voices with role-based resolution
  - generates a brand motif jingle for recurring brands from their sonic signature
  - builds a break from host intro, bumpers, one or more ad spots, and host outro
  - records per-spot campaign history (format, sonic signature, summary) for format rotation and campaign arc continuity

Every finished segment then passes a final **loudness-reconciliation** step: it is
measured (`measure_lufs`, EBU R128) and nudged with a single corrective `volume`
gain so music, hosts, beds, and ads all air at one integrated-LUFS target
(`[audio] lufs_target`, with ads at `ad_lufs_target` — 1 LU hotter). This holds
perceived volume steady across segment types regardless of which upstream filter
produced each one (the Green's `dynaudnorm` path has no fixed target on its own).
It is idempotent (an already-on-target segment skips the re-encode, so the
redundant terminal passes some segments take cost only a measure) and best-effort
(a measurement or re-encode failure leaves the segment untouched — never dead air).
A music **cache hit** replays a normalized file from a prior session and so bypasses
`normalize()` and this pass; the producer therefore calls `reconcile_cached_music()`
on each hit, which reconciles the cached file to the music target on first play and
stamps a `reconciled_lufs` marker into the norm sidecar so later hits skip both the
re-encode and the measure. This self-heals files cached before reconciliation
existed (which otherwise aired at their old, quieter level) one play at a time.

### Egress FX pipeline (the transmitter, applied last)

Every segment reaches the playback queue through one funnel —
`_enqueue_with_egress()` in `scheduling/producer.py` — so music, dialogue, ads, and
bridges all leave through a single chokepoint after every mix, concat, and
transition-sting merge is done. The funnel runs an ordered egress FX pipeline whose
optional final stage is the **FM broadcast chain** (`apply_broadcast_chain()` in
`audio/normalizer.py`): one extra FFmpeg pass that colours the finished audio like an
over-the-air FM signal — a gentle pre-emphasis HF shelf, the ~15 kHz channel band-limit,
and a flat loudness-offset trim (no stereo swirl, no dynamics). Voice and music exit
through the same final stage, so there is no "FM music next to studio-clean voice"
seam. Toggle it with `[audio] broadcast_chain` (default off — studio-clean) — or, on the HA add-on,
the **On-Air Sound** option (`MAMMAMIRADIO_BROADCAST_CHAIN`, env > toml) so operators
can switch to studio-clean without rebuilding the baked-in `radio.toml`. It is also
operator-toggleable **live** from the admin Engine Room On-Air Sound dial
(`POST /api/broadcast-chain`), which re-calls `configure_broadcast_chain()` to (dis)arm
the chain on the next produced segment — no restart, no queue purge — so an operator
can A/B the FM colouring against studio-clean on the live stream. A separate
pass with no `loudnorm` in-graph keeps the psymodel SIGABRT surface (3 equalizers +
loudnorm on ffmpeg 8.x / Pi aarch64) closed, and it holds the shared admission slot
from `mammamiradio.audio.admission` so the extra encode respects the Pi 2-FFmpeg
ceiling. The admission gate caps gated call sites at 2 ordinary/background jobs plus
1 rescue render in the steady state; that rescue cap is best-effort, not hard — a
wedged rescue render lets every subsequent rescue call proceed ungated too, so
concurrent rescue jobs aren't bounded at 1 for the duration of the wedge (see
`mammamiradio/audio/admission.py`). yt-dlp's own extract-audio ffmpeg runs outside
the gate (wrapping the download would hold a slot across a network fetch), so a
chart download can add one transient process on top of that ceiling.

The pipeline is **best-effort and instant-audio-safe**: a stage failure leaves the
prior audio in place and never raises, and emergency / bridge / rescue fills skip the
pipeline entirely so a dead-air rescue is never delayed by an extra encode (leadership
principle #2, INSTANT AUDIO). The skip is driven by an explicit `rescue` flag stamped
where each bridge/rescue is built (`_is_rescue_fill()`), **not** by sniffing overloaded
metadata keys. Packaged speech is restricted to the reviewed, content-addressed
manifest: approved recovery copy enters as rescue audio, while approved neutral
`banter/` copy remains ordinary banter. Welcome copy and unmanifested directory
discovery fail closed. The chaos and reactive-interference content stages slot in
**before** the broadcast chain — effects colour the content, the transmitter colours
the channel last.

**Colour-baking (repeat plays cost nothing on the Pi).** A norm-cache music hit is a
stable file that can air many times, and the FM pass is a full re-encode — expensive on
the Pi. So `_apply_egress()` bakes the coloured render once into the cache
(`_bake_cached_egress()`), keyed by source identity (path + mtime/size) +
`broadcast_chain_version()`. A filter/encoding change OR an in-place source rewrite —
`reconcile_cached_music()` re-levelling the norm file after a LUFS-target change, or an
evict-then-regenerate at the same path — yields a new key, so it re-bakes instead of
airing a stale colour. A replay — including the first play after a restart, since the bake persists on disk —
reuses the baked file with no encode; the bake is published atomically (encode to a
staging name, then `os.replace`) so a reader never sees a half-written file. Bakes are
evicted alongside `norm_` originals (the evict-last "processed audio" group in
`evict_cache_lru`, oldest-by-atime first, so a cold or stale-version bake goes before a
hot one); a bake currently queued for playback is passed in `protected_paths` so
eviction cannot pull it mid-stream. The trade-off is roughly double the per-track cache
footprint (a `norm_` original plus its `fm_` bake). One-shot ephemeral renders (fresh
voice/banter) have no stable identity to key on, so they are still coloured to a
per-play tmp.

**Synthetic layer cache.** Generated ad and imaging layers that do have stable
inputs are cached separately as `synth_*.mp3` under `cache_dir`: ad music beds,
environment beds, foley, brand motifs, transition stings, sweeper stings, and
synthetic talk-bed fallback. The key includes the synthetic kind, generator cache
version, normalized parameters (the rounded-up duration bucket is one such param),
MP3 output arguments, and variant. The cache publishes atomically through a hidden
MP3 staging file and copies hits back into the per-segment tmp file, so final ads,
spoken voice, and broadcast-chain renders stay one-shot. Tonal music beds, brand
motifs, and stings are deterministic; foley and synthetic talk-bed fallback rotate
through a small variant pool so repeated breaks do not expose one identical ambient
loop. Startup's suspect-file purge preserves `synth_` files even when they are short;
normal LRU eviction still treats them as regular cache files, evicting them before
`norm_`/`fm_` processed audio.

### Queue commit (the per-path gate matrix)

Every produced segment reaches the playback queue through a small set of commit
paths, and they DELIBERATELY differ in which gates they run — the differences are
the contract, not an oversight. Most segments commit in the `run_producer`
main-loop epilogue (the `if segment:` block); bridges and the startup prewarm
enqueue directly through `_enqueue_with_egress()`. The matrix below is pinned by
`tests/scheduling/test_queue_commit_contract.py`.

| Commit path | stopped discard | stale gate (playlist / chaos) | blocklist gate | egress (FM) | queue op | up-next shadow row |
|---|---|---|---|---|---|---|
| Main-loop commit (music + all generated speech: banter, news flash, ad, station-id, sweeper, time-check) | yes | **yes — pre-egress, shared epilogue** | yes\* (music only) | yes | append | **yes** |
| Operator air-next (forced trigger) | yes | **yes — same epilogue; a discard releases `operator_force_pending`** | yes | yes | **front-insert** (may drop the furthest-future tail, and unconditionally drops a stale-claim head†) | yes (at head) |
| Outer error-recovery rescue (`rescue=True`, built in the loop body) | yes | yes (epilogue) | yes\* | **skipped (rescue)** | append | **yes** |
| Inner bridge / drain-recovery rescue (direct enqueue) | yes | **no** — instant-audio: a fill must air regardless of source state | yes\* | **skipped (rescue)** | append | **yes** |
| Prewarm (startup pre-roll) | yes | **yes — source_revision + chaos epoch, checked after render AND post-egress** | yes | yes | append | **yes** |

- The **main-loop** stale gate compares `generation_revision` (captured once per loop
  iteration) against `state.playlist_revision` (and `chaos_cutover_epoch` against
  `generation_chaos_epoch`), and runs **pre-egress only** — those paths do not re-check
  after the awaited egress pass, so a slow/enabled egress colour pass widens their window.
- **Prewarm** keys on `source_revision` (bumped only by a true source switch via
  `switch_playlist`), not the broad `playlist_revision`, so a benign in-place edit
  (shuffle/add/move/enrich) keeps the on-source pre-roll. It also passes a **post-egress**
  `stale_check` to the funnel, so a switch landing during the egress encode discards the
  pre-roll at the last moment instead of putting it into the just-purged queue.
- Every successful playback admission publishes the same stable-id shadow row, so
  Scaletta contains only truly rendered audio while still showing startup prewarms
  and continuity bridges. The streamer reconciles that projection as it consumes
  the queue.
- \* The blocklist gate is the funnel's last-resort drop for a banned song that a
  mid-render ban race slipped past the ingest doorways (music only). It always drops
  the **audio** — a banned song never airs on any path — and every commit path
  propagates the funnel's drop-return so no shadow row or counter advance follows
  a mid-commit ban. The drop also must NOT overwrite the prior valid music bed:
  `state.last_music_file`, `producer._last_music_file`, and `_adjacent_music_source()`
  must all continue to reference the last successfully committed music track, not the
  dropped render (pinned by
  `test_blocklist_drop_on_main_loop_does_not_append_shadow_row`, #664).
- † A front-insert also drops the **queue head** outright (not just the
  furthest-future tail) when it carries a `transition_track_ref` — its "just
  finished playing" claim (baked into audio, crossfaded over the prior song's
  fade) is unconditionally broken the moment anything gets wedged ahead of it.
  Recorded as `GenerationWasteReason.STALE_PLAYED_TRACK_REF`; a fresh, accurate
  banter/ad-intro is produced on the next normal cycle.
- BANTER memory extraction is deliberately **not** a queue-time commit. The
  scriptwriter snapshots context, the producer rewrites that snapshot with the
  final aired lines including the transition, and the streamer schedules
  `memory_extractor` only after the send loop reaches EOF with bytes sent. Purged,
  skipped, stale, failed, or partial banter never writes persona or song-cue memory.

### Protected continuity reservations

Program-replacing controls — source switches, playlist purges, panic, and
Chaos/Festival cutovers — rebuild the real playback queue and its Scaletta shadow
in one synchronous operation. They reserve only audio already safe to play:
the packaged continuity clip first, then eligible normalized-cache music, then
the packaged `emergency_tone.mp3` when the clip and cache are unavailable. A
normalized-cache candidate passes the same final blocklist rule as every other
music admission, so a banned song cannot re-enter through this instant-audio
path.

Cache selection here shares the same rescue-rotation cooldown as the producer and
playback-gap rescues (`audio/norm_cache.py`): a cached song that aired as a rescue
within the last hour is deferred in favour of a fresher track, so repeated
controls do not keep reserving the same song. When every cached candidate is
still cooling, the reservation books the least-recently-heard one rather than
dropping to the emergency tone — real music always beats a tone. The cooldown is
fed only when a rescue is actually heard by a listener and resets on restart.

A successful replacement control supersedes an earlier reservation: it clears
ordinary and protected queued audio, clears any out-of-band `continuity_slot`,
and creates a fresh reservation for the new action. The resulting queue and
shadow projection therefore describe exactly the same final order. If no fresh
reservation can be built, the control fails closed instead: it keeps the first
immediately playable queued segment and any valid capacity-exempt slot, drops
only the remaining queued work to reopen producer capacity, and never cuts the
current segment into an empty runway. Every rebuild that drops queued work
advances `continuity_epoch`, including this conservative fallback, so an
in-flight render cannot refill the freed tail. An assetless control that cannot
mutate the queue leaves the epoch unchanged. Producer work and startup prewarm
capture that epoch and discard their result if it changed before queue admission,
including after egress.

After a continuity rebuild, tail adjacency is recomputed from the resulting
queue rather than retained from discarded work. Recovery audio and the emergency
tone are continuity breaks, so a following spoken segment cannot inherit a bed
or crossfade from music that the control action removed.

Last-known-good recovery and speech-bed candidates belong to the active
`StationState`. Normally rendered music becomes eligible only after successful,
current-epoch queue admission; discarded or stale renders never populate its
recovery index. Direct rescue or recycled fills become candidates only when their
own enqueue succeeds. A fresh or replacement station therefore starts without a
candidate and never inherits the producer's legacy process cache.

### Dynamic LLM routing (which model voices each task)

Script generation never names a model in code. Each call site asks for a model by
**role**, and `resolve_model()` in `mammamiradio/core/config.py` resolves it:

| Profile | Anthropic creative | OpenAI creative | Fast routes |
| --- | --- | --- | --- |
| Premium | `opus` | `large` | `haiku` / `small` |
| Balanced (default) | `sonnet` | `small` | `haiku` / `small` |
| Economy | `haiku` | `small` | `haiku` / `small` |

- `model_registry.toml` is the canonical place provider model IDs and token prices
  live: a per-provider `catalog`, a `routing` map (task→role), named `profiles`
  (the admin "quality dial": `premium` | `balanced` | `economy`), the OpenAI
  TTS model, and catalog-keyed pricing. `radio.toml` no longer owns model
  selection; a legacy `[models]` block is compatibility input only and emits a
  deprecation warning.
- `resolve_model()` is **total** — it tries the active profile, then
  `default_profile`, and returns `None` instead of raising when a registry route
  is unavailable. Callers degrade to stock copy or Edge TTS rather than making an
  arbitrary provider request. The only code-level default is the `creative` role;
  no model ID or price is baked into the application.
- A missing or malformed registry prevents provider calls and **degrades** to
  stock scripts and Edge TTS so the station always boots and airs; provider
  status reports that model routing is unavailable.
- `fast` (transitions and post-air memory extraction) is pinned to the lowest-latency model in every profile.
- The OpenAI fallback resolves the **same role** on the OpenAI side, so a transition
  falls back to the fast OpenAI model and banter to the creative one.
- `scripts/eval_openai_script_model.py` is a local, paid evaluator for that **OpenAI
  fallback** branch only. It runs parsed responses through the pure
  `hosts/segment_floor.py` receipt before any live-path sanitization/coercion: foreign
  prefix-form station names, non-roster named banter hosts, and missing spoken text are
  deterministic `PASS`/`FAIL`/`N/A` checks. This is raw model-output integrity, not a
  listener-output or Anthropic-quality claim. `direction` (playlist targets) and
  `memory_extract` (post-air control plane) are intentionally N/A. The command's
  `--dry-run` validates the corpus and previews paid-call bounds without provider access;
  deterministic unit tests, not an online evaluator run, enforce the contract in CI.
- The quality profile hot-swaps live via `POST /api/quality` (admin) with no restart
  and no queue purge — only the next generated segment changes model.

Every produced segment becomes a temporary MP3 on disk and is pushed into `asyncio.Queue[Segment]`.
Before queueing, `mammamiradio/audio/imaging.py` may prepend transition stings at music/speech boundaries and mix motif stings under sweepers. Optional operator assets live under `mammamiradio/assets/imaging/`; otherwise FFmpeg-generated stings and beds are used, with synthetic fallback renders reused through the `synth_` cache when their inputs match.

Bounded state lists (`played_tracks`, `running_jokes`, `segment_log`, `stream_log`, `ad_history`, `recent_outcomes`) use `deque(maxlen=N)` for automatic memory management — no manual truncation needed.

**Callback Director (cross-domain verbal gags).** A gag planted in DJ banter can resurface once inside an unrelated news flash or ad — a rare, cross-domain "callback". `hosts/verbal_gag_ledger.py` (`VerbalGagLedger`, in-memory, session-ephemeral) holds banter-seeded gags and reuses `home/gag_select.py`'s `weighted_offer` (the same weighted-pick + 0.55 silence roll that `home/evening_memory.py`'s `EveningLedger` uses for HA-event gags). Lifecycle, all at QUEUE time so a discarded segment never plants or burns a gag: banter's `new_joke {text, punch}` is stashed on `state.pending_verbal_gag` and committed to the ledger in the banter success callback; before a flash/ad the producer calls `offer(contrasting_to=...)` and passes at most one gag to the scriptwriter (which injects a "land this here" instruction, or omits the key entirely); the gag is hard-retired after one travel, and only when the generator reports it actually landed (`callback_used`). Durable listener persona and song-cue extraction are a separate post-air path, so queue-time gag bookkeeping can still happen without treating unheard banter as long-term memory. Flash/ad prompts no longer carry the full `running_jokes` list — `running_jokes` stays banter's self-reference + persona-store store.

**Evening running gags (HA-event callbacks).** `home/evening_memory.py`'s `EveningLedger` tallies repeated discrete home toggles across an evening and surfaces a deferred, approximate callback ("the coffee machine, on again tonight") into banter via the STASERA prompt block. Gag-candidacy is decided by device **domain** (not hardcoded entity_ids), so it works on any operator's home out of the box: `switch`/`fan`/`lock`/`vacuum`/`binary_sensor` toggles are gag-worthy, while `sensor`/`climate`/`media_player`/`weather`/`light` and `person.*` are not. Operators tune this via `[home.running_gags]` in `radio.toml` (`domain_allowlist` replaces the default domain set; `entity_allowlist` restricts to specific entity_ids; `entity_denylist` silences chatty entities) — parsed into `core/config.EveningGagsSection`, degrade-to-default on malformed input. An evening "session" ends after `EVENING_GAP_SECONDS` (3.5h) with no real home activity — `last_active` advances only on real activity (excluding numeric drift, `person.*`, device-availability flaps, and passive `weather`/`sun` changes), so neither radio-cadence polling nor passive environmental events can keep a quiet evening alive forever — or at the 4am day rollover.

**Moment Receipts (the durable trail behind ritual-recipe moments).** `home/moment_receipts.py`'s `MomentStore` records every Home Assistant ritual-recipe moment from match through confirmed air, so a listener can verify a home-triggered reaction was real and an operator can answer "why did the host say that" (or "why did nothing happen"). One recording model covers all live delivery lanes, because they all air through the next banter segment: match rows are recorded at the producer poll site (`elected`, or `dropped` with a reason — `directive_slot_busy`, `interrupt_slot_busy`, `interrupt_cooldown`); the row's opaque id travels to the consuming segment's metadata (`ritual_moment_id`: a consumed directive's id rides the scriptwriter's consume/restore handoff — the banter result, not live state, so a fresh HA poll mid-generation can't cross the wires — while an interrupt directive deliberately keeps its id in the pending slot until queue-commit, protected because new matches drop as `*_slot_busy` while it waits; `gag_moment_id` for ritual-sourced evening-gag buckets, whose `ritual_family` provenance threads `HomeEvent → GagBucket` and upgrades the bucket's label to the generic family label so a device name can never become a receipt label); `StationState.on_stream_segment()` flips the row to a provisional `airing` at send-start (rescue/fallback fills are guarded out — backup audio never claims credit for the house); and the playback loop's finally records the true outcome verbatim from `classify_stream_outcome` (`aired`/`skipped`/`no_listeners`/`not_streamed`/`fallback_rescue`), independent of the provenance ledger. A moment whose path to air dies later is demoted with the same honesty: `generation_failed` (stock-copy fallback or a post-consume render death), `canned_fallback` (a canned clip aired instead of the gag), `interrupt_override` (a live cut-in clobbered the waiting directive), `muted` (operator muted the entity mid-flight), and `restart` (`load()` demotes stale `elected`/`airing` rows, since neither the pending directive nor the airing finalize survives a restart). Persistence mirrors the evening ledger: `cache_dir/moments.json`, atomic write, corrupt-tolerant load, `_CACHE_PROTECTED`, capped at 100 rows with 7-day retention — and the disk write happens only at the producer's save site; streamer paths mutate in memory and set the dirty flag, so the playback loop never does JSON I/O. Surfaces: `/public-status` exposes `ha_moments.recent` (≤3 rows, generic `public_family_label` + coarse age only — no entity ids, confidence, or spoken lines on the unauthenticated endpoint; an `airing` row shows only while its segment is what `now_streaming` plays); the admin `/status` exposes the full trail as `moments_admin` (≤25 rows) behind admin auth. Every store call is best-effort and never raises into the audio path.

Chaos Mode adds three state fields around the existing queue model: `chaos_mode_active`, a typed `chaos_pending` first-strike slot, and `chaos_cutover_epoch`. Enabling the mode purges pre-produced lookahead segments, bumps the epoch so any in-flight pre-chaos segment is discarded at commit, and queues a chaos-flavored `BANTER` next. Disabling clears `chaos_pending` and bumps the epoch without purging already queued audio. `played_track_log` is a separate play-time history used by impossible-recall chaos prompts; it is populated in `on_stream_segment()` for music, not when music is merely queued.

### Studio atmosphere

Two features create the illusion of a live radio studio:

- **Studio bleed**: After producing a music segment, the producer mixes a faint (-22dB) snippet of a previously-played banter clip under ~35% of music segments. This creates the "someone left a mic on" feeling.
- **Humanity events**: A one-shot event system (cough, paper rustle, chair creak, pen tap) fires exactly once per session after 15+ segments have been produced. SFX files live in `mammamiradio/assets/demo/sfx/studio/` (inside the package so `mammamiradio/scheduling/producer.py` and packaging find them together).

### Clip sharing

A rolling `deque[bytes]` ring buffer on `app.state` records up to `CLIP_MAX_SEGMENT_SECONDS` (180s) of raw MP3 chunks during the playback loop. `POST /api/clip` extracts a shareable file into `{cache_dir}/clips/`: for a live ad or banter segment it captures the whole segment so far (operator-authored content, no copyright cap); for music it captures the last 30 seconds. When an ad/banter segment ends, the playback loop snapshots it so a tap within `CLIP_LOOKBACK_SECONDS` (15s) after it ends still grabs the whole bit. Clips are served without auth at `GET /clips/{id}.mp3` and auto-expire after 24 hours. Per-IP rate limiting (1 clip per 10 seconds, rolled back on a `no_audio` no-op so a cold-start listener can retry) and a 50-clip disk cap prevent abuse. On failure the route returns structured codes (`retry_after` seconds, or `reason: "no_audio"`) — never prose — which the listener UI maps to warm copy.

### Periodic chart refresh

When the playlist source is charts, the producer checks every 90 minutes and merges new chart entries into the live playlist without resetting `played_tracks` history. This prevents long sessions from looping the same track set.

## Playback and fanout

`streamer.py` owns the live station timeline.

- `run_playback_loop()` pops the next `Segment`, marks it live in `StationState`, and reads the MP3 in chunks.
- Chunk delivery is throttled to `config.audio.bitrate`, which is the single source of truth for stream pacing and ICY bitrate headers.
- `LiveStreamHub` fans each chunk out to all listeners.
- Slow listeners are dropped instead of stalling the whole station.
- Temp segment files are deleted after playback finishes.

Important design choice: there is one shared timeline. Listeners tune into the current live point, not their own private playback state.

### Delivery cushion (send-ahead pacing)

The playback loop does not offer each source packet to listener fanout exactly
at its real-time deadline. Source packets are capped at **125 ms** (3,000 bytes
at 192 kbps), so a private, persistent `StreamPacer` (in `streamer.py`, owned by
`run_playback_loop`) keeps a **500 ms send-ahead target** on one monotonic source
media timeline. At 192 kbps that is roughly the first four packets; after that,
the source-to-fanout schedule stays no more than one packet (625 ms) ahead. This
helps absorb a short event-loop or CPU scheduling pause — including one caused
by rendering a newly created station ID, ad, banter, or a Home Assistant
projection — before it reaches a direct listener (e.g. a Sonos player consuming
`/stream`).

The timeline is deliberately **continuous across natural segment boundaries**:
music → station ID → ad → banter → music share one origin, so the lead is not
re-accrued at each transition (which would add silence and drift). The pacer
resets only on a true discontinuity — no listeners (including a subsequent
mid-segment room refill), playback stop/resume, a real queue gap / fallback, or
an explicit skip — via a named `reset_timeline(reason)` call.

If a pause is longer than the whole lead, the pacer uses **at most a three-packet
recovery phase**, then rebases the pacing origin once and records the deficit as
an `overrun_rebased` event. At the default packet cap, that phase restores 375
ms; ordinary bounded packets may follow immediately until the 500 ms target is
rebuilt. It never sleeps a negative interval and never turns the missed
wall-clock history into an unbounded backlog of overdue chunks — the unavoidable
long stall stays audible, but it cannot compound into a second catch-up phase or
many seconds of stale playback. The packet cap changes source-read granularity
while leaving bitrate, ICY metadata, queue ordering, and overflow protection
intact. Because listener queues remain bounded by packets, their shorter packets
give a slow listener a tighter time budget before drop. The 500/625 ms bound
applies only to source-to-fanout pacing: after `LiveStreamHub` enqueues a chunk,
ASGI, socket, and client buffers can still delay physical playback. A skip or
status cutover therefore has no physical-audio latency guarantee; slow listeners
are dropped instead of stalling the station.

Pacing outcomes and completed-send outcomes feed the bounded private diagnostics
described under [Reading stream-delivery diagnostics](operations.md#reading-stream-delivery-diagnostics)
— exposed only through authenticated `/status`, never `/public-status`.

### Stream audio format metadata

External integrations should call `GET /public-status` before playback and read
`stream.audio_format` to declare the stream correctly. The object exposes
`codec`, `mime_type`, `bitrate_kbps`, `sample_rate_hz`, and `channels`. Use
`mime_type` and `bitrate_kbps` when declaring `/stream`.

`audio_format` is the station's **canonical/target encoding** — the format the
normalizer produces and the `/stream` response headers advertise. Bundled demo
and canned fallback assets are not guaranteed to be re-encoded to this format,
so players must rely on MP3 frame self-description for exact decode parameters
on a per-frame basis. The contract `audio_format` provides is the nominal one,
which is the same contract every ICY-headered internet radio publishes.

The canonical metadata is built once per response by
`mammamiradio/audio/stream_format.py::stream_audio_metadata(config)` and is the
single source feeding both the `/public-status` payload and the `/stream`
response headers (`Content-Type` and `icy-br`). The legacy
`stream.bitrate_kbps` field reads from the same helper output so it can never
diverge from `stream.audio_format.bitrate_kbps` in the same response.

## Capability flags

The system uses boolean flags in a frozen `Capabilities` dataclass (`mammamiradio/core/models.py`, with detection and serialization in `mammamiradio/core/capabilities.py`):

| Flag | Source | What it enables |
| --- | --- | --- |
| `anthropic` | `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` present | Live AI-generated banter and ads |
| `ha` | `HA_TOKEN` + integration enabled | Home Assistant API access is available |
| `home_context_ready` | `ha` is true AND a prompt-safe HA context slice has actually been fetched | Ambient home context in banter |

The dashboard derives a tier label from these flags: Demo Radio, Full AI Radio, Connected Home — reaching Connected Home requires an AI host key and `home_context_ready` (not just `ha`), so having a valid HA token isn't enough on its own until a real context slice populates. `GET /api/capabilities` returns flags, tier, and a guided `next_step` hint (what the user should do next).

## Music sources

`fetch_startup_playlist()` (in `mammamiradio/playlist/playlist.py`) walks this priority chain at boot and returns the first source that yields tracks. Each tier is independently gated; falling through to the next is silent (logged at INFO, not a warning).

1. **Persisted source** (any prior `cache/playlist_source.json` selection). Restored verbatim if loadable.
2. **Charts + local blend** (when `MAMMAMIRADIO_ALLOW_YTDLP=true`): up to 100 tracks fetched from Apple Music Italy RSS. MP3s in `music/` are merged in and deduplicated by `spotify_id`. Total catalog typically 100-300 tracks. `source_id="apple_music_it_top_100"`.
3. **Jamendo CC** (when `radio.toml` `[playlist].jamendo_client_id` is set): CC-licensed tracks via the Jamendo API. Default radio.toml ships `jamendo_country = "ITA"` + `jamendo_order = "popularity_week"`, so the resulting fetch is "Italian-trending" (artist nationality = ITA, sorted by current week popularity), and `jamendo_limit = 200` to keep the rotation pool deep. These fields are optional — empty country = no nationality filter; empty order = Jamendo's default sort; limit must be `1`-`200`. Fields are also overridable via `JAMENDO_COUNTRY`, `JAMENDO_ORDER`, and `JAMENDO_LIMIT` env vars. `source_id` is the tag string; the persisted source URL `jamendo://playlist?tags=…&country=…&order=…` encodes the source identity (tags, country, order) so reload via `/api/playlist/load` restores the same playlist selection. `limit` is NOT encoded in the persisted URL — it always comes from the active `radio.toml` / `JAMENDO_LIMIT` config at fetch time.
4. **Classic Italian eras** (explicit admin selection only): `classic://italian/70s`, `classic://italian/80s`, and `classic://italian/90s` resolve through yt-dlp search queries for cantautori/classic-pop eras. Each era stamps fetched tracks with a `year` hint (`1975`, `1985`, `1995`) so the admin playlist can render decade badges. Because this is an operator-selected source, failure raises an explicit toast instead of silently falling through.
5. **Local `music/` files** (always available when MP3s exist on disk): operator-supplied MP3s in `music/`. Loaded as a first-class source — yt-dlp is not required, and this branch fires whether or not Jamendo is configured. `source_id="local_music_dir"`.
6. **Bundled demo assets**: pre-shipped MP3s in `mammamiradio/assets/demo/music/`. Empty by default; populated optionally per the demo-asset contract.
7. **Built-in `DEMO_TRACKS`**: metadata-only Italian-flavored placeholder list. Last-resort fallback so the station always boots with something.

The admin Music & Coda controls expose reload buttons for charts/Jamendo when their capabilities are available and unconditional decade buttons for Anni '70, Anni '80, and Anni '90. `/status` returns a bounded playlist window (default 80 tracks, max 200) plus a `playlist_page` metadata envelope `{total, offset, limit, has_more, revision}`; the dedicated `GET /api/playlist` endpoint handles lazy load-more. Track objects carry `album_art`, `source`, `year`, and `youtube_id` so the browser can render thumbnails, source chips, and era pills without another round trip.

Once playback is running, the producer's recovery layers (packaged recovery clip, last-known-good music recycle, emergency tone, forced banter) keep the queue from starvation if a source disappears mid-session. Silent audio is never queued intentionally.

### Operator song blocklist

Because every source above is re-fetched fresh on startup, an in-memory "remove" would reappear after a restart. The operator blocklist makes a ban durable. It persists to `cache_dir/blocklist.json` as `{serialized_key: {display, banned_by, banned_at}}`, keyed by the single canonical identity `normalized_track_key(track) = (artist.strip().lower(), title.strip().lower())` (the same key used for playlist dedup, so a ban holds across sources even when the per-source track id differs). The store is best-effort and corrupt-tolerant: a missing or malformed file loads as empty and never raises into the audio path; writes are atomic (`tmp` + `os.replace`).

Enforcement is a single primitive, `playlist.filter_blocklisted(tracks, blocklist)`, applied at every doorway where tracks enter `state.playlist`: startup (`main.py`), source switch (`_apply_loaded_source`), the mid-session chart refresh (`fetch_chart_refresh`), and the external/listener download commit (`_commit_external_download`). The norm-cache **rescue** path is a separate doorway — it serves cached audio directly without passing through `state.playlist`, so `select_norm_cache_rescue` (in `audio/norm_cache.py`) drops blocklisted cache files itself, matching each file's `{title, artist}` sidecar against `state.blocklist`; a banned song never re-airs even when the queue starves and recovery kicks in (if every cache file is banned the rescue degrades to the next layer — canned clip / forced banter — never to a banned song). The external/listener commit returns a distinct `"banned"` status (not `"dropped"`): the admin gets an honest "it's banned" notice and a listener request fails loudly (`song_error`) instead of spinning on "searching…". Bulk `/api/playlist/enrich` honors the blocklist; only an explicit single `/api/playlist/add` bypasses it as an intentional override. Banning (`POST /api/track/ban`, or the per-row `/api/playlist/remove`) also clears a matching `pinned_track` and synchronously drops any not-yet-started queued segment of the song — the currently-airing segment finishes untouched, so a ban never causes dead air. The one path that **does** interrupt the airing song is the on-air console's **Ban** button (`POST /api/track/ban-now-playing`): it resolves identity from `now_streaming.metadata` (`artist`/`title_only`, falling back to parsing the `Artist — Title` label, so it bans even a rescue-cache or one-off song that never entered `state.playlist`), runs `_apply_ban` to purge queued copies, then reuses the exact skip path (`_request_skip`: listener-skip record, empty-queue bridge to forced music, `skip_event`, `now_streaming → skipping`). Ban precedes skip so the bridge reads the post-purge queue depth and still force-bridges to music if the ban emptied the queue — never dead air. It is starvation-exempt like the per-row ✕ Ban. A bulk ban that would leave fewer than `MIN_ROTATION_AFTER_BAN` songs (or that would empty an already-small pool) is refused with a warm message rather than starving the pool onto the rescue path; a single per-row removal stays exempt. The persist call is best-effort — when `blocklist.json` can't be written the ban still holds for the session and the API echoes `persisted: false` so the admin UI says "banned for now, may come back after a restart" instead of promising permanence. `POST /api/track/unban` and `GET /api/track/banlist` back the admin "Banned" manager. Listener thumbs-down voting is a separate later slice; this layer is operator-only.

### Operator song preferences

Operator song preferences are soft taste hints, not bans. They persist to `cache_dir/song_preferences.json` as `{serialized_key: {score, display, updated_at, updated_by}}`, keyed by the same `normalized_track_key(track)` identity as the blocklist. Scores are `1` for thumbs-up and `-1` for thumbs-down; clearing a preference removes the row. Loading is missing/corrupt tolerant and writes are best-effort atomic (`tmp` + `os.replace`) so a bad preference file cannot stop audio.

`StationState.song_preferences` is loaded at startup and exposed only on admin JSON. `POST /api/track/preference` accepts `vote: "up"|"down"|"clear"` plus exactly one target: `now_playing: true`, `index`, or `key: [artist, title]`. The route only mutates `song_preferences`: it does not call skip, purge queue entries, remove tracks, or change `state.blocklist`. `GET /api/track/preferences` lists the full rows and counts for the admin panel. `/status` includes only the current track's preference and playlist row scores; the full row list stays behind `/api/track/preferences`. `/public-status` and the Home Assistant now-playing APIs intentionally omit preferences.

Selection applies preference multipliers after hard eligibility filters inside `StationState.select_next_track()`: thumbs-up `x2.5`, thumbs-down `x0.15`, neutral `x1.0`. Pinned tracks, Move Next, listener requests, cooldowns, heading steering, Record Hunt, and bans keep their existing authority; preferences only bias among tracks already eligible for weighted selection, and the active Record Hunt heading lift is not suppressed by an older thumbs-down. The norm-cache rescue path stays preference-free on purpose: after dropping blocklisted files and avoiding recent/current identities when possible, it picks the simplest safe cache bridge so recovery remains fast.

## TTS architecture

Each host declares a TTS engine in `radio.toml`: `engine = "edge"` (default), `engine = "openai"`, `engine = "azure"`, or `engine = "elevenlabs"`. Dedicated ad voices and the sonic-brand sweeper voice use the same provider-routing fields.

**Edge TTS** (Microsoft): free, no API key. Each host maps to an Azure Neural voice (e.g., `it-IT-GiuseppeNeural`). SSML prosody tags (rate, pitch) are derived from the host's personality axes for voice differentiation.

**OpenAI TTS**: requires `OPENAI_API_KEY` and uses the separately configured
`[tts.openai]` registry entry. Each host maps to an OpenAI voice (e.g., `onyx`).
Personality-aware delivery instructions are generated from the host's energy,
warmth, and chaos axes — the model interprets these as acting direction, not
just static parameters.

**Azure Speech TTS**: requires `AZURE_SPEECH_KEY` and `AZURE_SPEECH_REGION`. Useful for official Italian voices and HD voices while keeping the existing Edge voice family as fallback.

**ElevenLabs TTS**: requires `ELEVENLABS_API_KEY` and operator-provided voice IDs. V2 (`eleven_multilingual_v2`) remains the default for ads, sweepers, guest bits, and every host that has not explicitly opted in. Marco and Giulia use `eleven_v3` with a code-owned `delivery_profile`; V3 accepts only `stability`, never V2-only similarity, style, or Speaker Boost controls.

For selected normal host banter, the script carries one semantic cue beside —
never inside — the clean spoken text. Marco may be `energetic`, `curious`, or
`playful`; Giulia may be `dry`, `curious`, or `playful`. Only the V3 TTS boundary
maps those values to provider audio tags. Ads, news, IDs, sweepers, transitions,
time checks, stock/fallback/repair lines, V2, and Edge receive no tag. The clean
line remains the sole input to transcript metadata, safety/language guards,
memory, accounting, and any Edge fallback, so a failed V3 request cannot make a
fallback voice read markup aloud.

Fallback chain: cloud TTS failure or missing credentials →
`edge_fallback_voice` (so the role falls back to its own Edge voice, not a
stranger) → the house Edge fallback → `TTSUnavailableError`. The final failure
deletes partial speech files and lets required voice reach the producer's
music/continuity rescue ladder; it never substitutes generated silence for
speech.

A session's blended TTS estimate records a confirmed paid-provider response before local raw-file I/O or normalization. If that local processing later fails and the role falls back to Edge, the session still includes the paid request; missing credentials, provider errors, and Edge-only synthesis remain uncounted. This is a conservative session estimate, not invoice-level provider reconciliation.

A singleton OpenAI client is reused across OpenAI TTS calls for connection pool efficiency.

## Compounding station memory and truthful listener sessions

`core/listener_session.py` maintains an in-memory, identity-free station epoch. The stream hub remains authoritative for raw HTTP connection membership, while the session state machine records only station-level presence:

- A `0 → 1` active-listener edge starts an epoch.
- Reconnects and empty periods shorter than 600 seconds resume that epoch.
- At least 600 continuous seconds with no active listeners starts the next epoch.
- State resets on process restart. No cookie, account, IP/UA fingerprint, or migration identifies a listener.

`persona.py` maintains the durable station-session counter in SQLite (`cache/mammamiradio.db`). Each epoch creates a process-unique receipt in the append-only `listener_session_receipts` ledger; the producer retries with bounded backoff and acknowledges the in-memory epoch only after the receipt and persona update commit together. A retry after an ambiguous post-commit interruption observes the same receipt and cannot increment twice, including when an older process overlaps a restart. The process token still lets an in-memory epoch number reused after restart represent a distinct durable event. Raw connection telemetry remains operational: `/status.listeners.total` and the admin-only `connections_total` are cumulative HTTP stream connections, not unique people.

The persona tracks motifs, open station theories, running jokes, callbacks, and an arc phase derived from the committed station-session count. Ordinary banter may receive aggregate `<station_memory>`. Listener-session context is absent unless the producer has atomically claimed the one companionship cue available after 1,800 seconds of active listening in the current epoch. Only active-listening time accumulates; an empty grace period contributes zero. The cue prompt contains only a coarse duration bucket (`30-44`, `45-59`, `60-89`, or `90+` minutes) and a fixed identity-free instruction—never an epoch, connection count, exact duration, receipt, or identity.

The cue lifecycle is `UNAVAILABLE → AVAILABLE → ATTEMPTED → QUEUED → CONSUMED` or `ABANDONED`. Only a naturally scheduled ambient banter break may claim it; operator/Chaos/urgent/Home/directive/request/release/ritual/recovery/fallback lanes cannot. Accepted generated copy must return matching proof fields and pass application-owned aggregate-companionship and exact-bucket content checks before the segment receives `listener_session_epoch` and `listener_session_cue="companionship"`. The queue admission boundary marks it queued synchronously. Generation, TTS, quality, admission, purge, stop, queue removal, overflow, fallback, or stale-epoch failure permanently abandons the claim, and stock fallback copy remains untagged.

Playback verifies the stamped epoch before the segment and before every audio chunk. A mismatch is discarded through `GenerationWasteReason.LISTENER_SESSION_STALE` before that chunk reaches the hub. `LiveStreamHub.broadcast()` reports how many listener queues accepted the chunk; only the first positive acceptance moves the cue to `CONSUMED` and publishes now-playing state. The central `StationState.record_discard()` boundary owns abandonment for all unstarted queue cleanup, and the queue shadow verifies the pulled `queue_id` before removing a row, rebuilding from the real bounded queue if the projection ever drifts.

Hosts may build shared station mythology, but may not turn a stream connection into an arrival, return, or identity claim. The final producer boundary checks the assembled transition plus banter text in English and Italian; it makes one bounded identity-free repair attempt and falls back to deterministic safe copy if the repair remains unsafe. A separately authorized, named Home Assistant resident-return fact is line-bound to its source entity; a door unlock or generic presence signal never grants that authority.

The hot `write_banter` contract does not write persona memory. Instead, `scriptwriter.py` creates a `MemoryExtractionCommit` snapshot, `producer.py` replaces its draft lines with the final aired script, and `streamer.py` schedules `hosts/memory_extractor.py` only after the banter segment finishes sending cleanly. The extractor then asks the fast script model for bounded `persona_updates` and applies them under a write lock. This post-air fast-lane call is automatic whenever generated banter has station-memory metadata and airs cleanly; there is no separate per-call opt-out beyond disabling the persona store or removing script-provider credentials.

Instruction-like patterns in persona entries are filtered before storage (matching the `ha_context` sanitizer) to prevent stored prompt injection across sessions.

Packaged speech is a separate fail-closed boundary. `assets/demo/spoken_assets.json` declares each discoverable recovery/banter/welcome MP3 by relative path, SHA-256, kind, language, and reviewed transcript. Missing, unlisted, changed, malformed, or truth-unsafe speech invalidates the inventory. Runtime playback admits approved recovery and neutral banter speech; welcome copy and unmanifested directory discovery remain disabled. The release-invariants gate validates this manifest.

Anonymous listener-session diagnostics and legacy aggregate listener counters appear only on authenticated `/status`. `/public-status` retains its existing schema and exposes neither session diagnostics, cue metadata, nor listener counters.

## Song cues

`song_cues.py` builds machine-derived per-track memory in SQLite (`cache/mammamiradio.db`), separate from the persona:

- **Anthem detection**: a track played 3+ times and never skipped becomes an anthem. The cue is stored with confidence "anthem".
- **Skip-bit detection**: a track skipped 2+ times gets a skip-bit cue. When the listener skips a known skip-bit track, the hosts can react ("caught you again").
- **LLM reaction cues**: after a generated banter segment airs cleanly, `hosts/memory_extractor.py` can extract one free-text reaction cue for the pinned current track (e.g., "sempre questa canzone sul tramonto"). These are stored and reinjected into future banter prompts for that track.

Cues appear in banter prompts as a `TRACK MEMORY` block alongside operator-flagged rules from `track_rules.py`. The `youtube_id` from the producer-side queued music history is pinned in segment metadata and used to key cues after extraction rather than trusting LLM echoes, preventing orphan rows from hallucinated IDs.

Cue text is sanitized via `_sanitize_prompt_data` on the read path before injection, closing a cross-session prompt injection vector.

## Optional Home Assistant context

If `[homeassistant].enabled = true` and `HA_TOKEN` is present:

- `home/authorization.py` is the R0 choke point. A cold install receives only synthetic `sun.ambient` plus `weather.ambient` when exactly one raw `weather.*` source exists and has a valid condition, explicit C/F unit, and temperature; temperature is converted to Celsius and grouped into 5-degree bands. Zero or multiple weather sources yield no weather. Source labels, exact readings, forecasts, locations, areas, residents, and every other HA entity are discarded before downstream matching.
- a pre-R0 database keeps the established household feature set through a bounded legacy bridge. `home/migration.py` requires matching durable sidecar and DB-local install-origin witnesses; after an exact migration-only 35-ID manifest is observed, it seals only manifest version/digest, app version, and time. It never persists raw states or labels. Sidecar loss is recovered from the DB witness, while malformed or disagreeing witnesses and transplanted provenance fail narrow.
- authorization mode travels with every `HomeContext`. Fresh, stale, timeout, and module-cache paths reject a context stamped for the other mode. Hard mutes apply both to a raw ambient source and to its synthetic ID.
- narrow mode skips registry/name loading, generated labels, event diffs, radio-event and ritual matchers, timer interrupts, mood derivation, weather forecast arcs, first-home directives, evening gags, and Moment Receipt projections. `/public-status` and `/status` do not replay persisted household moments, and the manual label-regeneration route reports no candidates.
- exact-manifest sealing runs once at a time in a tracked background thread so file and directory `fsync` calls never stall the producer event loop. Only an authoritative legacy install receives that observer.

- `ha_context.py` polls the Home Assistant REST API state snapshot on the configured prompt-context interval (default 300s, disable with `ha_context_enabled = false`) and filters it through a default-deny privacy layer
- sensitive domains (`device_tracker`, `camera`, `alarm_control_panel`), free-text helper domains (`input_text`, `text`), and telemetry/config entities are excluded before prompt assembly
- `person.*` is kept as home/away presence only (GPS, `user_id`, and tracker attributes stripped) so the empty-home mood and explicitly sourced named-resident facts can work; person events never reach `/public-status`
- allowed entities are scored by domain salience, recent changes, area metadata, event activity, and curated-label overrides
- the prompt receives a bounded top slice (12 entities by default, capped at 2000 characters) rather than the full home snapshot
- hand-tuned entity labels (curated tier) remain authoritative; unknown entities resolve through a generated catalog backed by Anthropic (`home/catalog.py`, cached locally), then a sanitized HA display name plus area metadata, and are dropped entirely rather than letting a raw entity ID reach a host prompt
- event diffing, mood classification, and weather narrative arcs continue to feed the existing scriptwriter fields
- home mood uses the heuristic ladder by default; an experimental LLM scene-namer can be enabled with `MAMMAMIRADIO_HA_MOOD_LLM=true`, caches names for `MAMMAMIRADIO_HA_MOOD_TTL_SECONDS`, and falls back to the ladder whenever disabled, unavailable, slow, or invalid
- 7 reactive triggers fire on specific state changes (coffee machine, door unlock, vacuums, verified named-resident transitions, terrace lights); door unlock copy remains identity-neutral and cannot infer who entered
- banter references are tiered: 1 item by default, up to 2 when a mood scene is active (mood counts toward cap)
- `home/context_director.py` turns the casual ambient slice into one selected, opaque `PromptFact`: an explicit allowlist covers weather, climate, vacuum, sun, and curated coffee; room-presence needs a per-entity opt-in. It groups weather/climate temperatures into one topic, reserves a fact only after queue admission, starts its 30-minute cooldown at stream start, and releases only an unstarted discarded reservation. Reactive directives, rituals, and weather flashes remain separate programming lanes.
- the director's `home_fact_*` metadata is internal. `/status` receives only count-based `home_context_director` diagnostics; `/public-status`, queue projections, now-playing metadata, and stream logs remove it recursively.
- weather-mood fusion allows hosts to connect outdoor conditions to indoor activity
- the weather news flash grounds itself in the real Home Assistant forecast when available, then spins it into absurd local color; with no forecast (HA disconnected or unsupported) it falls back to the fully fictional meteo prompt, so the segment never goes silent. `NEWS_FLASH` shares the same HA-context refresh gate as banter/ad, so the flash reads a freshly refreshed forecast (bounded by the weather cache TTL plus one poll interval) rather than the startup snapshot. The arc follows the station language: Italian stations use `state.ha_weather_arc`, every other language uses `state.ha_weather_arc_en` — never the Italian arc — and the stock fallback line is localized too
- numeric state passthrough in `ha_enrichment.diff_states()` ensures power sensors generate events
- the listener dashboard shows a "Casa" card with mood, weather, recent events, and the "Live from your home" strip of recently aired home moments via `ha_moments` (incl. `recent`) in `/public-status`
- the admin panel shows full HA details (mood, weather arc, events summary, pending directives, scored entities, and privacy filter counts) via `ha_details` in `/status`, plus the Moment Receipts trail via `moments_admin`
- scored entities and privacy filter counts are admin-only and never appear in `/public-status`
- `push_state_to_ha` always sets `entity_picture` on `media_player.mammamiradio` to an absolute http(s) image: the track's cover (`Track.album_art`) while a song plays, and the station logo for host talk, ads, music with no cover, and idle/stopped. The logo fallback is required because HA's media-control card does not clear a removed `entity_picture` — it keeps the last cover — so omitting it would leave the previous track's art on screen during a news flash. The logo URL is `[brand] artwork_url` (absolute http(s) only; relative paths are rejected because HA resolves `entity_picture` against its own origin), defaulting to the bundled station logo. `media_image_url`/`media_image_remotely_accessible` are intentionally omitted (inert for a state pushed via the REST API rather than a media_player integration component)

### Isolated HA projection

A full `/api/states` reply can carry a few thousand entities. Decoding that JSON
and running the entity-map, authorization, mute, filter, label, score, diff, and
audit projection over it is CPU work that used to run inline on the same asyncio
loop as `run_playback_loop` — a completed refresh could therefore block egress
long enough to be heard by a direct listener.

The retained producer-owned HA request now keeps only its **transport and
enrichment I/O** (`/api/states`, optional registry, optional weather) on the
event loop. Once the raw response bytes and enrichment values are available, JSON
decoding plus the pure projection run in one module-owned
`ThreadPoolExecutor(max_workers=1)` (`ha-projection` thread in `home/ha_context.py`).
The worker receives copied, inert request values plus the cache-directory path,
reads its own detached label-catalog snapshot, and returns only a candidate
(`_HomeContextProjectionCandidate`). It never touches `StationState`, module
caches, persistence callbacks, event baselines, or any logging that contains HA
values.

The coordinator (`_HAContextRefreshCoordinator` in `producer.py`) stays the sole
owner of request lifetime, stage state, mute/authorization revalidation on the
loop, stale-result discard, observed-entity bookkeeping, and safe-boundary
adoption at `_drain_completed_result`. A cancelled, timed-out (30 s total cap),
closed, or superseded request's worker value is ignored — it can never publish
after coordinator close or after a newer request, and no extra refresh begins
while the retained request still owns the mailbox. The single worker serializes
an abandoned calculation and the next one; they never run concurrently.

The coordinator also stamps a **coarse, privacy-safe stage** on `StationState`
(`states_request`, `enrichment_wait`, `projection`, `idle`, cleared on every
terminal/cancel/close path) via `set_ha_context_refresh_stage`. It is diagnostic
metadata only — never a prompt input or a scheduling control — and is surfaced in
the `/status` stream-delivery diagnostics so one late-packet event can be joined
to the projection phase without retaining any household data.

## Album cover artwork

`Track.album_art` drives the now-playing artwork on every surface. The primary,
already-wired surface is the listener PWA MediaSession (`web/static/listener.js`),
which shows the cover on the phone lock screen, CarPlay, and Control Center; Home
Assistant's `entity_picture` (above) is a secondary surface.

- **Chart tracks** read their cover straight from the Apple charts RSS feed item
  (`artworkUrl100`, upscaled to 600px) in `playlist.py` — no extra network call.
- **Searched/added and listener-requested tracks** carry a YouTube thumbnail from
  the yt-dlp search; `playlist/cover_art.py` upgrades it to a real cover via the
  iTunes Search API (`country=IT`) on the background download path
  (`_commit_external_download`), off the event loop. Results are cached to
  `cache_dir/cover_art_cache.json` (hashed key; definitive misses cached with a TTL,
  transient failures never cached). Resolution is best-effort and never raises into
  the audio or HA-push path; a miss falls back to the existing art or the station logo.

This is opportunistic context, not a hard dependency. Failures there should not stop the station.

### Timer interrupt flow

When a HA timer fires, the station immediately interrupts playback with a pissed/urgent host segment:

```text
HA timer fires (timer.xyz → idle, with recent finished_at)
    ↓
ha_context.py: lightweight 5s poll detects idle transition (separate from the default 300s full-state prompt-context fetch).
    Cancel/reset filter: only fire when finished_at is set and within the last 30s.
    ↓
check_reactive_triggers() → InterruptSpec(directive, urgency, cooldown)
    ↓
producer.py: _fire_interrupt(state, spec, queue, skip_event)
  1. Drain lookahead queue (no buffered music leaks between bridge and banter)
  2. state.ha_pending_directive = spec.directive
  3. state.chaos_pending = ChaosSubtype.URGENT_INTERRUPT  (pissed tone)
  4. state.chaos_cutover_epoch += 1
  5. skip_event.set()  ← skips currently playing segment
  6. Load alert.mp3 from assets/sfx/, or generate a short tone → state.interrupt_slot
     (best-effort; never blocks the skip)
    ↓
run_playback_loop: interrupt_slot checked before queue.get() → bridge plays (≤2s)
    ↓
Producer generates URGENT_INTERRUPT banter with directive (async, LLM)
    ↓
Pissed banter plays after bridge
```

Timer interrupts are configured via `[[homeassistant.timer_interrupt]]` blocks in `radio.toml`. The dedicated timer poll reads those entity IDs without mutating the module-level HA entity lists.

The same mechanism is callable directly via `POST /api/interrupt` (admin auth, 60s cooldown) — any HA automation can inject a custom directive without `radio.toml` configuration.

## Access model

### Route table

Write routes that consume request details use `mammamiradio.web.json_body.read_json_object`.
Empty, malformed, or top-level non-object bodies return `422` with
`{"ok": false, "error": "<human message>"}` before endpoint-specific validation runs.
Admin auth dependencies still run before body parsing on protected routes.

| Route | Method | Access | Description |
| --- | --- | --- | --- |
| `/` | GET | Public | Listener page. Over trusted HA ingress the admin panel is served instead. |
| `/listen` | GET | Public | Alias of `/` for backwards compatibility |
| `/admin` | GET | Admin | Admin control room panel |
| `/dashboard` | GET | Admin | 301 redirect to `/admin` (legacy) |
| `/sw.js` | GET | Public | PWA service worker |
| `/static/{filename:path}` | GET | Public | PWA static assets (manifest, icons) |
| `/favicon.ico` | GET | Public | Browser default favicon path; serves the station icon SVG |
| `/stream` | GET | Public | Infinite MP3 stream |
| `/healthz` | GET | Public | Liveness probe with process uptime |
| `/readyz` | GET | Public | Readiness probe with queue depth and startup status |
| `/public-status` | GET | Public | Current segment, recent log, the real queued segments only (`upcoming_mode` is `queued` when render-ready audio exists and `building` when no render-ready segment exists yet), and `stream.audio_format` (the canonical encoding contract — see "Stream audio format metadata" below) |
| `/status` | GET | Admin | Full admin JSON: queue depth, uptime, scripts, `consumption` (session AI cost estimate, unpriced-model flag, and fixed-key cost breakdown for host scripts, transitions, ads, post-air memory extraction, and TTS), anonymous `listener_session` diagnostics (epoch, phase, active duration, pending persona count, and companionship cue state), HA context, errors, `provider_health`, `runtime_status` (normalized provider state, session failover event history, `bridge_health` rescue-bridge telemetry, `rescue_rotation` cached-music cooldown telemetry, `producer_headroom` readiness, bounded `render_timings` diagnostics, and `continuity_slot` — the admin-only projection of any reserved capacity-exempt safety audio, `{label, duration_sec, audio_source, reservation_id}` or `null` — see operations.md), `production` (the live "In produzione" feed — `current` is the phase the producer is building right now, `recent` is a bounded trail of just-finished work; admin-only, never in `/public-status`), `current_track_preference`, `moments_admin` (Moment Receipts full trail, ≤25 rows — see "Moment Receipts"), and `playlist_page` (`{total, offset, limit, has_more, revision}`). Accepts `?playlist_offset=0&playlist_limit=80` (max 200) for lazy loading. |
| `/api/setup/status` | GET | Admin | First-run setup status, detected run mode, station mode, canonical `guided_setup` stages, and a render-ready `guided_setup.strip` payload |
| `/api/setup/recheck` | POST | Admin | Re-run setup probes |
| `/api/setup/provider-check` | POST | Admin | Active, secret-safe Anthropic/OpenAI/Azure Speech/ElevenLabs connectivity check |
| `/api/setup/addon-snippet` | GET | Admin | Copy-friendly Home Assistant add-on config snippet |
| `/api/homeassistant/context-candidates` | GET | Admin | Sanitized Home Assistant context preview for onboarding; includes additive `entities` rows while preserving legacy arrays, and is never exposed on `/public-status` |
| `/api/homeassistant/entity-policy` | PATCH | Admin | Apply exactly one idempotent `muted` or `personal_moment_enabled` property to one Home Assistant entity; the response includes effective consent, policy revision, and the count of matching queued host breaks removed by a mute or a personal-moment consent revocation |
| `/api/shuffle` | POST | Admin | Shuffle playlist |
| `/api/skip` | POST | Admin | Skip current segment |
| `/api/track/ban-now-playing` | POST | Admin | Ban the airing song by identity and skip it (the one interrupting ban path) |
| `/api/track/preference` | POST | Admin | Set or clear an operator song preference with `vote: "up"\|"down"\|"clear"` plus one target: `now_playing: true`, `index`, or `key: [artist, title]`; the Admin playlist sends the existing key target so a refreshed row cannot redirect the vote, while the index target remains compatible for existing API clients; never skips, purges, or mutates the blocklist |
| `/api/track/preferences` | GET | Admin | List operator song preference rows and up/down counts |
| `/api/purge` | POST | Admin | Remove queued segments |
| `/api/queue/remove` | POST | Admin | Remove one queued segment by stable `id` (or legacy `index`) |
| `/api/playlist/remove` | POST | Admin | Durably ban one rendered rotation row with `{revision, index, id}`; success returns the new `playlist_revision` |
| `/api/playlist/move` | POST | Admin | Reorder two rendered rotation rows with `{revision, from, from_id, to, to_id}`; success returns the new `playlist_revision` |
| `/api/playlist/move_to_next` | POST | Admin | Pin one rendered rotation row as upcoming with `{revision, index, id}`; success returns the new `playlist_revision` |
| `/api/playlist/add` | POST | Admin | Add a track to the playlist |
| `/api/playlist/load` | POST | Admin | Load a playlist by URL |
| `/api/hosts` | GET | Admin | List hosts with personality settings |
| `/api/hosts/{host_name}/personality` | PATCH | Admin | Patch host personality axes (energy, warmth, chaos) |
| `/api/hosts/{host_name}/personality/reset` | POST | Admin | Reset host personality to defaults |
| `/api/pacing` | GET | Admin | Current pacing configuration |
| `/api/pacing` | PATCH | Admin | Patch pacing fields (songs between banter, ad spots per break, etc.); malformed bodies return 422, values are clamped to safe floors/ceilings |
| `/api/setup/save-keys` | POST | Admin | Save API keys via dashboard |
| `/api/capabilities` | GET | Admin | Capability flags, tier, next-step hint, connect status, and provider degradation telemetry |
| `/api/chaos` | GET | Admin | Return `{"enabled": bool}` for Chaos Mode |
| `/api/chaos` | POST | Admin | Toggle Chaos Mode with `{"enabled": bool}`; persists `chaos_mode_active` to `.env` or HA add-on options |
| `/api/party` | GET | Admin | Return `{"active": bool, "mode": str\|null}` for Festival Mode |
| `/api/party` | POST | Admin | Toggle Festival Mode with `{"action": "enable"\|"disable", "mode": "festival"}`; persists `festival_mode` to `.env` or HA add-on options; purges queue and arms first-strike banter on enable |
| `/api/quality` | GET | Admin | Return `{"active_profile": str, "profiles": [str]}` for the model quality dial |
| `/api/quality` | POST | Admin | Set the active model profile with `{"quality_profile": "premium"\|"balanced"\|"economy"}`; hot-swaps live with no restart and no queue purge; persists `MAMMAMIRADIO_QUALITY`/`quality_profile` |
| `/api/trigger` | POST | Admin | Trigger segment production |
| `/api/stop` | POST | Admin | Gracefully stop the session (skip + purge + pause producer until `/api/resume`) |
| `/api/resume` | POST | Admin | Resume a stopped session |
| `/api/credentials` | POST | Admin | Update credentials at runtime |
| `/api/clip` | POST | Public | Capture a shareable clip (full ad/banter segment, or last 30s of music) |
| `/clips/{id}.mp3` | GET | Public | Serve a saved clip (no auth, for sharing) |
| `/api/track-rules` | POST | Admin | Flag a reaction rule for the current track |
| `/api/listener-request` | POST | Public | Submit a song request or shoutout |
| `/public-listener-requests` | GET | Public | Sanitized listener-request feed for the on-page sidebar (`public_token`, `status`, name, message, type) — admin `request_id`, `submitter_ip_hash`, and `evict_after` stay server-side |
| `/api/listener-requests` | GET | Admin | List pending listener requests (full record including `request_id`, `status`, `evict_after`) |
| `/api/listener-requests/dismiss` | POST | Admin | Dismiss a pending listener request by `ts` (legacy) or `request_id` (canonical) |
| `/api/playlist` | GET | Admin | Paginated playlist window; `?offset=0&limit=80` (max 200); returns `{tracks, total, offset, limit, has_more, revision}` with each admin track carrying an opaque row `id` and its current `preference` score |
| `/api/search` | GET | Admin | Search playlist and external sources; pagination via `offset`/`limit` (max 50 local, max 10 external) and `external_offset`/`external_limit`; `include_external=false` skips yt-dlp when the client has exhausted web results; every response (including an empty query) returns the playlist `revision` captured with the local snapshot before any external lookup, and each local result carries its opaque row `id` |
| `/api/heading` | POST | Admin | Steer the next music stretch with an era seed (`{"seed": "classic://italian/80s"}`) or free text (`{"text": "2000s female vocals"}`); no queue purge |
| `/api/direction` | POST | Admin | Free-text alias for heading direction (`{"text": "sunday morning italian"}`); expands to song targets, searches metadata, and downloads targets in background |
| `/api/heading/clear` | POST | Admin | Clear the active heading/direction and return to automatic rotation without removing blended tracks |
| `/api/playlist/add-external` | POST | Admin | Add external track from search results; accepts optional `album_art` URL (http/https only, validated server-side) |
| `/api/interrupt` | POST | Admin | Immediately interrupt the stream — hosts deliver pissed/urgent banter with a custom directive. Body: `{"directive": str, "urgency": "pissed"\|"urgent"\|"gentle"}`. 60s cooldown enforced; returns 429 on spam. |
| `/api/hot-reload` | POST | Admin | Reload `prompt_world.py`, `transitions.py`, `fallbacks.py`, `station_name_guard.py`, then `scriptwriter.py` (leaves-first) in-place via `importlib.reload()` — stream continues uninterrupted, next banter uses new code. Requires `--workers 1`. `memory_extractor.py` is deliberately excluded — it holds live in-flight task/apply-lock state a reload would reset mid-extraction. |

Rotation-row mutations use optimistic identity checks rather than trusting a
position by itself. The `id` fields above are opaque Admin row tokens, not song
identity: callers must echo the revision, position, and token(s) from the same
rendered snapshot. Missing or malformed fields return `422` with
`reason: "invalid_target"`. If the revision or
the token at either submitted position no longer matches, the server returns
`409` with `reason: "stale_playlist"`; if a source/rotation update already owns
the mutation boundary, it returns `409` with `reason: "rotation_updating"`.
Neither conflict mutates the rotation. Search pagination similarly rejects
mixing pages from different revisions in the Admin client, and late search
responses are accepted only for the query generation that started them.

### Auth rules

Admin access is granted by one of:

- localhost access, unless `ADMIN_PASSWORD` is configured
- HTTP Basic auth via `ADMIN_USERNAME` and `ADMIN_PASSWORD`
- token auth via `X-Radio-Admin-Token` header for non-local requests when only `ADMIN_TOKEN` is configured
- private-network trust (LAN, Tailscale, HA Supervisor) when no credential is configured — reads allowed, writes CSRF-checked; public IPs are always rejected

In standalone mode, a non-loopback bind without a credential is rejected during config validation. The HA add-on is exempt: it boots on `0.0.0.0` with no credential and trusts its own LAN. The full matrix is the single source of truth in [operations.md](operations.md) ("Admin access model").

### CSRF protection

Mutating admin requests (POST/PUT/PATCH/DELETE) over non-loopback networks must pass a CSRF check. The dashboard injects a per-session token via `__MAMMAMIRADIO_CSRF_TOKEN__` placeholder replacement. Requests are allowed if any of: the CSRF token header matches, the Origin or Referer is same-origin, the request uses token auth (`X-Radio-Admin-Token`), or the request comes through HA ingress. Loopback clients are exempt.

### Source switch concurrency

`source_switch_lock` (asyncio.Lock on `app.state`) serializes source imports and
replacement. Admin row mutations make a bounded attempt to enter that same
boundary: a busy lock returns the recoverable `rotation_updating` conflict;
after admission, the route revalidates its revision, index, and opaque token(s)
before mutating without another await. Source replacement requests immediate
cutover only after fresh protected replacement audio is admitted: the current
segment is skipped and playback begins from the new source. If the continuity
fallback preserves an older queue head or slot, or no ready runway exists, the
current segment finishes and the response reports `skipped: false`. The producer
uses a `playlist_revision` counter on `StationState` to detect and discard
segments generated for a stale source. `/api/shuffle` also increments
`playlist_revision` so any in-flight producer work targeting the old order is
discarded and rebuilt against the new sequence.

Source replacement also follows the protected-continuity reservation contract
above. A successful fresh replacement supersedes existing reservations and
fallback slots. If no fresh replacement audio is ready, the current segment is
not cut and the last safe prior-source runway remains in place; the source
revision still prevents a render begun for the prior source from being admitted
after the switch.

## Failure model

This repo is biased toward "keep the station on air."

- producer exceptions never crash the app or queue generated silence — a rescue ladder tries packaged recovery audio, then norm-cache music, then the last-known-good music file, then a bounded branded recovery sweeper, then an emergency tone as the final rung; packaged recovery clips are non-ephemeral package resources and every producer/playback segment-cleanup path guards `mammamiradio/assets/demo/` before unlinking; the segment carries `error_recovery: True` (classified as fallback/rescue audio by `core/segment_status.py`) and `rescue: True` (skips the egress FX pass so the rescue is instant); if even the tone fails to generate the producer logs and retries on the next loop iteration rather than queueing silence
- script generation failures fall back to OpenAI when configured, then to stock copy; a temporary Anthropic overload or rate limit briefly benches its writer (respecting a bounded `Retry-After` when present) so affected later segments go straight to OpenAI, then retry Anthropic automatically after the short cooldown
- chaos first-strike script failures use subtype-specific stock lines and report `provider_health.chaos.last_degraded_reason = "script_fallback"`; chaos audio failures are counted separately as `audio_failure`
- required speech fails closed: if every configured provider and Edge fallback is unavailable, partial files are removed and `TTSUnavailableError` reaches the producer rescue ladder; owned dialogue, ID, time-check, and ad fan-outs settle before scratch cleanup, while optional promo tags may still be omitted
- missing yt-dlp falls back to local files or demo tracks
- missing Home Assistant context is ignored
- missing ad brands disables ads rather than killing startup
- a missing, stale, or corrupt restart handoff manifest (`cache/restart_handoff/`) is a silent no-op — startup falls through to the normal cold-start rescue ladder instead of failing

The rich path is richer, but the failure path still produces a stream.

## File map

| Path | Responsibility |
| --- | --- |
| `mammamiradio/main.py` | app startup/shutdown and background task wiring |
| `mammamiradio/core/config.py` | `radio.toml` and `.env` loading plus validation |
| `mammamiradio/core/models.py` | shared dataclasses for tracks, segments, ads, and station state |
| `mammamiradio/core/capabilities.py` | Capability flags, tier derivation, and next-step hints |
| `mammamiradio/core/setup_status.py` | First-run setup status classification (legacy; retained for `/api/setup/status` compat) |
| `mammamiradio/core/sync.py` | SQLite database initialization and schema migration |
| `mammamiradio/playlist/playlist.py` | Charts, local, and demo playlist loading |
| `mammamiradio/playlist/downloader.py` | local-file, yt-dlp, and unavailable-source music handling |
| `mammamiradio/hosts/memory_extractor.py` | Post-air banter memory extraction for persona updates and LLM reaction cues |
| `mammamiradio/playlist/song_cues.py` | Machine-derived per-track memory: anthem detection, skip-bit detection, stored reaction cues |
| `mammamiradio/playlist/track_rationale.py` | "Why this track?" rationale generation for listener UI |
| `mammamiradio/playlist/track_rules.py` | Per-track personality rules flagged by admin via `/api/track-rules` |
| `mammamiradio/scheduling/scheduler.py` | pacing rules and upcoming preview |
| `mammamiradio/scheduling/producer.py` | segment generation pipeline |
| `mammamiradio/scheduling/clip.py` | WTF clip extraction from ring buffer, save, cleanup |
| `mammamiradio/release_campaign.py` | Packaged release-beat manifest loading and bounded on-air campaign state (`cache/release_campaign_ledger.json`) |
| `mammamiradio/restart_handoff.py` | Post-restart music continuity spool: producer writes safe recent segments, startup admits them into the queue (`cache/restart_handoff/`) |
| `mammamiradio/hosts/scriptwriter.py` | Anthropic/OpenAI prompts for banter and ad copy (TODO: split — see cathedral plan PR 6) |
| `mammamiradio/hosts/prompt_world.py` | Prompt-fiction data: expression banks, host fingerprints, style directives, Chaos/Festival mode blocks |
| `mammamiradio/hosts/transitions.py` | Transition rewrite openers + anti-repeat stem/massage helpers |
| `mammamiradio/hosts/fallbacks.py` | Stock fallback copy: chaos stock lines, ad-break intros/outros |
| `mammamiradio/hosts/persona.py` | Listener persona: compounding memory, arc phases, motif tracking, session counting |
| `mammamiradio/hosts/context_cues.py` | Time-of-day and cultural context for prompts |
| `mammamiradio/hosts/ad_creative.py` | Brand and voice selection, campaign-spine sampling for ad breaks |
| `mammamiradio/audio/imaging.py` | station imaging selector for transition stings, sweeper stings, and talk beds |
| `mammamiradio/audio/synth_cache.py` | reusable `synth_*.mp3` cache for generated ad/imaging layers |
| `mammamiradio/audio/normalizer.py` | ffmpeg helpers for normalization, mixing, tones, bumpers, bleed, and SFX |
| `mammamiradio/audio/audio_quality.py` | Audio quality gate: duration and silence checks before segments reach the queue |
| `mammamiradio/audio/tts.py` | TTS synthesis (Edge, OpenAI, Azure Speech, ElevenLabs) |
| `mammamiradio/audio/voice_catalog.py` | Edge, OpenAI, and curated Azure voice ID catalogs |
| `scripts/audition_tts_voices.py` | Local audition clips and manifest generation for configured/catalog TTS voices |
| `mammamiradio/home/ha_context.py` | Home Assistant polling, heuristic mood classification, optional LLM scene-namer, reactive triggers |
| `mammamiradio/home/catalog.py` | Generated device-label catalog: curated overrides, Anthropic-backed generation, four-tier resolver |
| `mammamiradio/home/ha_enrichment.py` | Pure HA event derivation: state diffing, event pruning, numeric passthrough |
| `mammamiradio/web/streamer.py` | HTTP routes, playback loop, clip endpoints, listener fanout (TODO: split — see cathedral plan PR 5) |
| `mammamiradio/web/status_payload.py` | Shared admin/listener status payload serializers re-exported by `streamer.py` |
| `mammamiradio/web/auth.py` | Request-layer admin auth: `require_admin_access`, CSRF enforcement, trusted-network classification |
| `mammamiradio/web/listener_requests.py` | Listener-request endpoints (submit, public feed, admin queue, dismiss) and the song-wish download background task |
| `mammamiradio/web/og_card.py` | Open Graph share-card PNG renderer |
| `mammamiradio/web/templates/` | `admin.html`, `listener.html`, `clip.html` |
| `mammamiradio/web/static/` | CSS, JS, icons, manifest, service worker |
| `mammamiradio/assets/` | `logo.svg`, `demo/` (bundled MP3s + SFX) |
| `start.sh` | local dev entry point with uvicorn and reload |

## Deployment models

The app runs in three modes:

- **Local dev** via `start.sh` (uvicorn with --reload)
- **Docker container** via `Dockerfile` / `docker-compose.yml` (runs as non-root user, persistent `/data` volume)
- **Home Assistant add-on** via `ha-addon/mammamiradio/` (Alpine-based, Supervisor injects HA token, ingress proxies the dashboard into the HA sidebar)

The ingress-compatible UI uses JavaScript base path detection so the dashboard works both at `/admin` and behind HA's ingress proxy.

## Operational notes

- Version metadata lives in `pyproject.toml`.
- Generated assets land in `tmp/` and `cache/`.
- Station state is in memory. Restarting the process resets counters, logs, and running jokes.
