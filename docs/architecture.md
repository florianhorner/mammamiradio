# Architecture

`mammamiradio` is one FastAPI process with one shared station timeline in memory.

One background task stays ahead and produces segments. Another reads the next ready segment and streams it to every connected listener at real playback speed.

A fresh install awaiting audible First Listen proof has one client-local step
in front of that shared timeline: `/stream` emits a reviewed, packaged mini-show
and only then subscribes that client to `LiveStreamHub`. Required proof is
hearing that stream on this device; Home Assistant speaker dispatch stays an
optional later route. The asset starts with ready MP3 bytes, so it adds no
startup render or network dependency. Because it never enters
`asyncio.Queue[Segment]`, existing listeners, now-playing state, and the
producer remain untouched. Completed and pre-feature installs go straight to
the live hub.

## Runtime overview

```text
operator local files -----\
                           +-> local-or-starter base playlist
attributed starter catalog/
                           ^
standalone external-media-/  (optional; absent from both add-ons)
                |
                v
          StationState + scheduler.py
                |
                v
        producer.py renders/adopts Segment files <--- transient Jamendo provider
                |                                  (optional; one lease/artifact)
                |
                v
          asyncio.Queue[Segment]
                |
                v
   streamer.py playback loop -> LiveStreamHub
                |                    |
                |                    +-> completed/pre-feature /stream clients
                |                    +-> fresh + unheard /stream clients,
                |                        after their packaged mini-show finishes
                |
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

The listener revalidates `/public-status` with a weak semantic ETag; live/idle/stopped tabs poll every 3/3.5 seconds when visible and 30/60 seconds when hidden.
Matching validators return bodyless 304s while one monotonic anchor advances listener clocks; payload, anchor, and ETag share a generation guard.

## Startup flow

In Home Assistant add-on mode, Supervisor's stored app options are the durable
authority for admin modes and pacing. Supervisor materializes
`/data/options.json` as a generated, read-only startup projection; the runtime
reads that projection but never writes it directly. A value selected only in
process memory by a pre-fix build cannot be reconstructed after an upgrade
rematerializes an older Supervisor value.

`mammamiradio.main:startup()` does ten things:

1. Loads `radio.toml` and `.env` through `config.py`.
2. Validates the config and applies legacy migration like `station.bitrate -> audio.bitrate`.
3. Purges suspect cache files (< 10 KB, likely failed downloads), scans the cache, trims the configured ceiling to what the disk can hold through `_disk_safe_cache_ceiling_mb`, and evicts old entries to the effective limit.
4. Captures the install-scoped Home context boundary before SQLite initialization, then cross-checks its sidecar witness with a redundant DB-local witness after initialization. Missing, corrupt, or disagreeing R0 witnesses fail narrow; a cold install can therefore never become legacy merely because its database exists on a later boot.
5. Restores an eligible persisted base selection. A retired `jamendo://` source is rewritten to the current base; add-on-external selections cannot restore extractor authority. Without an eligible selection, operator-owned local `music/` files win when present, otherwise the hash-pinned attributed starter catalog is the offline base.
6. Initializes the clip ring buffer for WTF clip sharing.
7. Restores `chaos_mode_active` from `MAMMAMIRADIO_CHAOS_MODE` or the HA add-on's Supervisor-generated, read-only `/data/options.json` startup projection without arming a first strike.
8. Creates shared app state, then synchronously admits any safe, receipted,
   non-Jamendo `cache/restart_handoff/` music segments straight into the queue
   (see "Restart handoff spool" below) — before the background producer/playback
   tasks start, so a listener connecting right after an update can reach an
   already-normalized track instead of an empty queue.
9. Launches:
   - `run_producer()` to fill the lookahead queue
   - `run_playback_loop()` to stream queued audio
10. Logs a one-line boot summary with resolved config dir, audio source, API key presence, HA status, and track count.

### First Listen state and source truth

First Listen keeps setup progress separate from runtime authorization and from
the shared audio queue:

- `core/first_listen.py` stores policy-free facts in
  `cache/state/first_listen_receipt_v1.json`: the human audible confirmation,
  the attempt it is bound to, and completion of the privacy review. The privacy
  choice itself is stored by the normal configuration path, never in this
  receipt.
- The receipt carries two proof kinds in one unchanged v1 shape. Required proof
  is **browser-local**: `record_listener_heard` writes a `listener_*` attempt
  with no selected entity, where acceptance and hearing are the same moment.
  The **Home Assistant** kind remains readable for installs that completed
  onboarding before browser-local proof existed: a selected speaker plus an
  opaque HA-accepted attempt, with the human confirmation bound to it.
- Home Assistant acceptance is compare-and-swap state: verification must
  present the current attempt id, and a newer playback supersedes older proof.
  A completed listener proof is terminal and is never superseded, so a later
  Home Assistant playback cannot overwrite it
  (`record_accepted_playback` returns early on `is_complete_listener_proof`).
  Browser-local confirmation presents no attempt id at all.
- If the audible moment happened but the receipt write failed, the app keeps
  only a process-local recovery handle so **Restore sound check** can retry the
  same fact without replaying audio.
- Feature-era install origin uses two agreeing witnesses: the owner-only
  `cache/state/first_listen_install_origin_v1.json` sidecar and the private
  `_mammamiradio_first_listen_install_origin_v1` SQLite table. Missing, corrupt,
  or disagreeing evidence projects to `unknown`; only a proven pre-feature
  install bypasses the listening/privacy onboarding.
- Origin migration and receipt loading run as background tasks after the
  producer and playback tasks are scheduled. Filesystem work runs off the event
  loop, and a failure leaves setup incomplete/narrow without delaying audio.
- `core/first_listen_show.py` selects the reviewed packaged mini-show only for a
  fresh install without audible proof. The `/stream` generator sends it directly
  to that client before joining `LiveStreamHub`; it never becomes a `Segment` or
  changes shared now-playing state.

`StationState.source_readiness` is event evidence, not a filesystem scan on each
status request. The `golden_path.source_readiness` object from `/status` and the
`guided_setup.source_readiness` object from `/api/setup/status` project five
human-facing rows (charts, Jamendo, local, bundled demo music, recovery) with these exact states:
`on_air`, `playable`, `candidates_only`, `configured_unchecked`, `unavailable`,
`not_configured`, `not_bundled`, and `cover_only`. Recovery on air proves the
transport is audible; it never makes `programming_ready` true by itself.

### Restart handoff spool

`mammamiradio/restart_handoff.py` owns a small durable spool the producer writes to and startup reads from, purely to shorten the gap between an add-on update finishing and the first listener hearing live programming again:

- After each ordinary music segment is queued, the producer (`scheduling/producer.py::_schedule_restart_handoff_spool`) best-effort copies it (hash-addressed, content-verified) into `cache/restart_handoff/segments/` and atomically publishes a small `manifest.json` describing up to `DEFAULT_MAX_ENTRIES` (3) recent, already-normalized, non-ephemeral music tracks. One-shot listener-request handoff segments are excluded and clear any older manifest, so a promised song cannot replay without its dedication after a restart. Older/unreferenced spool files are pruned on each write; files still queued for playback this session are protected from that prune.
- On the next boot, `main.py::_admit_restart_handoff` loads and validates the manifest (`admit_restart_handoff_entries`) — checking file existence, size, SHA-256, age (`DEFAULT_MAX_ENTRY_AGE_SEC`, 6h), and the operator blocklist — and enqueues whatever passes validation before the producer or playback loop has started. A corrupt, stale, or missing manifest is a silent no-op; the normal cold-start rescue ladder (see `docs/operations.md`) still applies underneath it.
- Skipped entirely when `session_stopped` is set (the station was deliberately stopped, not just updated) so a stopped station doesn't quietly start playing again.
- This is independent of, and does not replace, the norm-cache/demo-asset rescue ladder described in `docs/operations.md` — it is a *faster* first source when it has something to offer, not a new failure mode when it doesn't.
- A hard kill between a spool write's `mkstemp` and its final `os.replace` can leave an orphaned `.manifest-*.tmp` (in `cache/restart_handoff/`) or `.handoff-*.tmp` (in `cache/restart_handoff/segments/`) scratch file behind. `prune_stale_handoff_tmp_files` sweeps both directories at the start of every boot (before `_admit_restart_handoff` runs), deleting only scratch files older than 6h, with path-containment and symlink checks so a corrupted/symlinked cache dir degrades to a no-op rather than raising, and a per-directory cap (500 candidates, oldest first) so a pathological backlog can't stall startup. A second, independent ceiling (5000 candidates) bounds the raw `glob()` enumeration itself, so even an extreme backlog can't make the scan/sort step unbounded before the 500-candidate prune cap gets a chance to apply.

### Release beat campaign

`mammamiradio/release_campaign.py` turns an optional packaged `mammamiradio/assets/release/release_beat.toml` manifest into a bounded, listener-safe on-air announcement after an update:

- The manifest ships disabled/absent by default — no file, or `enabled = false`, means the feature is a complete no-op and nothing changes. `scripts/validate-release-beat.py` validates its schema and listener-safe copy in CI (see `docs/runbooks/ha-addon.md` → "Release invariants gate").
- When enabled, `ReleaseCampaign` (loaded once at startup, persisted to `cache/release_campaign_ledger.json`) offers the scriptwriter a release-beat prompt block on the first eligible banter break; `hosts/scriptwriter.py` decides whether it actually made it into the spoken lines (`release_beat_used`).
- Delivery is only counted once a listener queue actually accepts audio (`_emit_release_campaign_result` in `web/streamer.py`, reading the same Tier-3 stream-result hook the provenance ledger uses). This accepted-listener boundary also covers someone joining after the segment-start sample; a connected listener that rejects every chunk, a queued-but-discarded segment, and a skipped or partial-read segment do not spend one of the campaign's `max_airings`.
- The campaign self-retires on its own budget (`max_airings`, default 5) or time window (`campaign_window_seconds`, default 72h), independent of whether Show Memory (the provenance ledger) is enabled.

### Heading overlay

The admin Rotazione tab can steer the next stretch of music without replacing the
base playlist:

- `POST /api/heading {"seed": "classic://italian/80s"}` may load one of the
  external classic-era sources only in a standalone install with the effective
  `external-media` capability. It filters the operator blocklist, dedupes against
  the live pool, tags newly blended tracks with the active `Heading.id`, and bumps
  `playlist_revision` once. Both add-ons omit that capability and return the
  locked actionable `403` boundary instead of starting an extractor.
- `POST /api/direction {"text": "2000s female vocals"}` (also accepted as
  `/api/heading` with `text`) expands operator text into concrete `{artist,title}`
  targets and first searches the current local/base playlist. Matching tracks are
  retagged immediately. Only an effective standalone `external-media` capability
  may continue into metadata resolution and background downloads through
  `_commit_external_download`; add-ons keep the local matching behavior without
  acquiring external bytes. It never pins, purges, or blocks the live queue, and
  a late standalone download commits only if the source revision and active
  heading still match.
- `POST /api/heading/clear` is manual Back to auto. It clears `StationState.heading`
  and deletes `cache/heading.json`; already blended tracks remain in rotation and
  age out naturally. There is no purge and no audio interruption.
- `/api/playlist/load` is a true source replacement and clears the active heading
  plus `heading.json`, so a restart cannot reapply an old course over a freshly
  loaded base.

Steering is durable: an active heading keeps lifting its matches until the
operator clears it, and `selection_spent` is telemetry rather than a brake. That
makes a small found set the thing to guard. At the target share, a set of H tracks
brings any one of them back roughly every `H / share` picks, which for a handful of
tracks falls inside the plain repeat cooldown's blind spot and reads to a listener
as the same song again. So `select_next_track` cools a course track against the
rest of its own set instead: it excludes the last `H - 1` distinct course tracks
aired, so one cannot return until the others have had a turn. The set keeps its
full share of the show and cycles through instead of repeating. The exclusion is a
strict filter that relaxes with the others, and it never removes more than `H - 1`
of `H`, so a course can never starve selection.

`cache/heading.json` is an overlay, separate from `playlist_source.json`. Reads are
corrupt/missing tolerant and return no heading rather than failing boot. Seed
headings persist the seed; text directions persist concrete targets, phase, safe
counts, and Record Hunt narration throttle fields. Restore splits by kind to honor
INSTANT AUDIO: a **seed** heading still restores synchronously during startup
(re-fetch the source, re-tag matching tracks, blend new ones at the back of the
rotation; on empty/failure it deletes `heading.json` and continues in auto). A
**text direction** does not perform network resolution on the boot path — startup
re-tags any target already present in the freshly-fetched base and marks the course
active immediately. In a standalone process with effective `external-media`, it
may defer target resolution and downloads to a background task
(`_restore_direction_targets_background`) dispatched *after* the producer/playback
tasks start; add-ons never schedule that extractor work. Until
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
  - plays the packaged starter derivatives directly (they are already normalized and hash-verified), or normalizes operator-owned local files before queueing; all twelve starter tracks complete before a starter repeat
  - may accept a one-play Jamendo artifact only from the transient provider's lease boundary. A miss falls through immediately to local/starter music; the artifact is deleted after playback or cancellation and is never cache/rescue/handoff material
  - uses chart/classic/external candidates only in a standalone install with the effective `external-media` extra. Both add-ons omit the distribution, module, and executable
  - after eligible non-starter music lands, may launch a background prefetch that normalizes the predicted next track so it is ready before queue drain. A running prefetch is left to finish rather than cancelled and replaced — cancelling cannot stop its in-flight executor FFmpeg, which would keep holding the background admission slot (see [Egress FX pipeline](#egress-fx-pipeline-the-transmitter-applied-last)) while a replacement parks another thread behind it; the next music segment retries with a fresh candidate
- `BANTER`
  - asks Claude (or OpenAI as fallback) for structured dialogue JSON
  - synthesizes one line per host via the configured TTS engine (see [TTS architecture](#tts-architecture) below)
  - passes generated host speech through the imaging layer so banter and news use eligible adjacent music first, then a selected-pack talk bed, then a synthetic pad on cold starts
  - preserves running jokes in `StationState`
  - snapshots the generated evidence needed for station/song memory, but persists it only after the final aired banter script has streamed cleanly
  - when Chaos Mode is active, applies the per-call `CHAOS_MODE_BLOCK` and one `ChaosSubtype` prompt fragment while keeping the segment type as `BANTER`
- `AD`
  - picks brands with recurrence weighting and recent-brand avoidance
  - selects one of 6 ad formats: classic pitch, testimonial, duo scene, live remote, late-night whisper, or institutional PSA
  - resolves a sonic world and a named scene recipe for every shipped brand
  - casts speakers by role — duo scenes and testimonials use two distinct voices with role-based resolution
  - uses one quiet bed and at most two timed dry cues for a resolved recipe, keeping only speech and pauses from the LLM output so generic SFX and legacy motifs cannot layer on top
  - preserves generated brand motifs for legacy/custom campaigns with no recipe and for configured recipes that cannot resolve
  - builds a break from host intro, imaging-pack bumpers/SFX/beds when available, one or more ad spots, and host outro
  - records per-spot campaign history (format, sonic signature, summary) for format rotation and campaign arc continuity

### Exact-once music-to-speech handoffs

When a generated banter, impossible moment, news flash, or ad intro follows a
queued normal music segment, the station may keep the host-over-outro effect
without replaying the song. The invariant is deliberately strict: **every
playable music sample belongs to exactly one emitted segment**. Decoder-only
MP3 reservoir context may be duplicated in a scratch input, but those preroll
samples are trimmed before the tail is mixed and are never emitted twice.

1. Before touching the queue, the producer indexes the actual queued/egressed
   MP3 with the same validated ID3/Xing-skipping boundaries used by playback.
   It writes a frame-aligned head plus a decoder-tail input containing bounded
   reservoir preroll. The logical tail still begins at the head ownership
   boundary; the normalizer sample-trims the preroll before mixing. Malformed,
   short, stale, rescue, fallback, and unsupported files fail closed to
   ordinary dry/generic speech.
2. It renders both the tail-mixed speech and a dry/generic fallback. At one
   no-await queue mutation, it rechecks that the exact unstarted music object
   is still the queue tail, replaces it with the shortened head (including its
   real queue-shadow duration), and appends the tail-bearing speech successor.
   `has_music_tail` becomes true only at this paired commit, not when a render
   happened to find a song file.
3. The private pair is reconciled by every queue rewrite. Before playback,
   removing the successor or breaking adjacency while the head remains restores
   the full music predecessor and drops the successor. Explicitly removing the
   head instead honors that removal: it drops the successor without putting the
   song back. Once the head starts, a clean EOF marks its successor due before
   the active pointer is cleared; ordinary rewrites preserve that due successor
   and place Air Next immediately after it. Skips and destructive source,
   chaos, ban, purge, interrupt, or Stop controls cancel the successor. The pair
   and its scratch artifacts are never serialized into public status or restart
   state.

The normalizer receives only the bounded decoder-tail artifact and trims its
recorded preroll by sample count before mixing; it never uses `-sseof` or
`last_music_file` to seek back into a full song. The rest of a host break uses a
packaged or synthetic talk bed, never the outgoing song from its beginning.
Restart-handoff spooling also ignores a shortened private head, preserving only
ordinary full music entries for a future boot.

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
`mammamiradio/audio/admission.py`). The transient Jamendo provider buffers the
size-capped HTTP response before taking a background admission slot. It then
sends that buffer through a nonblocking pipe to its FFmpeg worker and normalized
partial file. Paced network waits stay outside shared admission. A standalone
`yt-dlp` extract-audio
FFmpeg remains outside that gate because wrapping its network fetch would hold a
slot across download; it can add one transient process only when the operator has
installed and enabled `external-media`. Neither add-on has that process surface.

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

### Modern Night Drive imaging pack

`mammamiradio/assets/imaging/` is the default imaging root for the standalone
app and HA add-on. Both shipped `radio.toml` files leave
`[imaging].assets_dir = ""`, so the add-on does not need a separate asset copy.
Only the station ID and sweeper use the Neon Relay signature. Velvet Horizon
defines the production style for the remaining cues and beds. Ad breaks have
separate `in`, `mid`, and `out` bumpers. Operators still use the existing
compatibility filenames. The pack contains 47 stereo MP3 files at 48 kHz, each
with its own retained project-authored source. Nine recipes define the ad scenes.

The schema-v2 `manifest.json` defines the runtime pack. It records asset and
source paths with their checksums. It also records each layer's timing, gain,
DSP, and license metadata, plus the selected design and checksum inventory.
The separate listening board stores the pack-scoped approval receipt. The
installed manifest omits that receipt and the board previews. CI and add-on
validation check the installed files against the inventory. `ATTRIBUTION.md`
is generated from the runtime ledger. The exact inventory is in
[`manifest.json`](../mammamiradio/assets/imaging/manifest.json); the local
audition procedure is in
[Operations](operations.md#audition-the-modern-night-drive-imaging-pack).

Setting `[imaging].assets_dir` replaces the packaged root with a custom root.
When the custom root lacks an asset, the runtime uses its procedural or cached
fallback. It does not read the missing asset from the packaged root. Within the
selected root, a transition tries `stingers/{from}_{to}.mp3` before the generic
directional stinger. Talk beds use eligible adjacent music first, followed by a
bundled bed or synthetic drone. For ads, a configured `[ads].sfx_dir` takes
priority over the selected root's `sfx/` directory. A resolved recipe uses its
declared bed and no more than two cue files. A missing or corrupt recipe falls
back without downloading or rendering source audio.

The runtime reads recovery audio from `mammamiradio/assets/demo/` and honors the
explicit `rescue` flag. Bridge and rescue fills still skip the egress pipeline.
The optional FM broadcast chain is independent. When enabled,
`[audio].broadcast_chain` colours the finished normal segment after the pack has
been mixed in.

### Queue commit (the per-path gate matrix)

Every produced segment reaches the playback queue through a small set of commit
paths, and they DELIBERATELY differ in which gates they run — the differences are
the contract, not an oversight. Most segments commit in the `run_producer`
main-loop epilogue (the `if segment:` block); bridges and the startup prewarm
enqueue directly through `_enqueue_with_egress()`. The matrix below is pinned by
`tests/scheduling/test_queue_commit_contract.py`.

| Commit path | stopped discard | stale gate (playlist / chaos) | music eligibility gate | egress (FM) | queue op | up-next shadow row |
|---|---|---|---|---|---|---|
| Main-loop commit (music + all generated speech: banter, news flash, ad, station-id, sweeper, time-check) | yes | **yes — pre-egress, shared epilogue** | yes\* (music only) | yes | append | **yes** |
| Operator air-next (forced trigger) | yes | **yes — same epilogue; a discard releases `operator_force_pending`** | yes | yes | **priority insert** (behind existing urgent warnings; may drop the furthest-future eligible tail, and unconditionally drops a stale-claim segment at the insertion point†) | yes (at priority position) |
| Outer error-recovery rescue (`rescue=True`, built in the loop body) | yes | yes (epilogue) | yes\* | **skipped (rescue)** | append | **yes** |
| Inner bridge / drain-recovery rescue (direct enqueue) | yes | **no** — instant-audio: a fill must air regardless of source state | yes\* | **skipped (rescue)** | append | **yes** |
| Prewarm (startup pre-roll) | yes | **yes — source_revision + chaos epoch, checked after render AND post-egress** | yes | yes | append | **yes** |

- The **main-loop** stale gate checks `source_revision` on its own axis, then treats
  `state.playlist_revision` as a cheap pre-filter: a bump only discards when
  `_music_segment_left_rotation` confirms the rendered song is genuinely gone from
  `state.playlist` (removed or banned). Ten of the thirteen sites that bump
  `playlist_revision` are benign — add, shuffle, move, enrich, direction retag — and
  a pool that merely grew leaves the render exactly as playable as when it started.
  Speech and rescue fills are never bound to a rotation row, so no playlist edit
  discards them. `chaos_cutover_epoch` and `continuity_epoch` are unchanged. The gate
  runs **pre-egress only** — those paths do not re-check after the awaited egress
  pass, so a slow/enabled egress colour pass widens their window. `_enqueue_stale_reason`
  re-checks at admission with the *same* predicate; `test_epilogue_and_admission_stale_predicates_agree`
  pins the two together so they cannot drift.
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
- The same music-eligibility gate holds every matched listener song until its
  dedication banter is admitted — including a song that already owns
  `pinned_track`. Reservations cover pending requests, the active handoff, and
  admitted-token tombstones, so archiving a request cannot release an unmarked
  equivalent already buffered ahead of its dedication. Successful acknowledgement
  creates one transient, request-scoped handoff: its token follows the promised
  recording through selection and queue admission, marks only that admitted
  segment as allowed through, and retains the reservation until playback claims
  it. Queue discard and borrowed-metadata stripping release the same tombstone.
  A retryable render or admission failure retains the handoff; a permanently
  unavailable source, a source switch without its matching dedication already
  on air, or a ban revokes it.
  `pinned_track` has its own monotonic ownership revision. A listener plan may
  borrow an independently owned same-recording operator pin, but its abandon,
  dismissal, and handoff-revocation paths can clear only the revision that the
  listener request actually claimed.
  The handoff also records the dedication banter's queue id: a purge revokes the
  promise only when it actually discards that still-queued dedication. Once
  playback dequeues it, the handoff normally remains valid; an unreadable file
  before the first byte is the exception and settles the same dependencies. If an
  exclusive promised song has already joined the queue behind that dedication,
  it carries the same queue id and is discarded with the removed announcement.
  Music selected from a newer same-recording operator pin instead stays queued
  with the old request metadata stripped. Forced and urgent banter do not claim
  listener requests because their priority insertion could separate a dedication
  from its promised song; the request waits for an adjacency-preserving ordinary
  break. If an operator MUSIC force coincides with an active handoff, the promised
  segment fulfills it through normal atomic admission behind the dedication rather
  than front-inserting ahead of the spoken promise. If an assetless source switch
  preserves that exact queued dedication as continuity runway, the switch drops it
  before revoking the requested recording, so the announcement cannot survive its
  song promise; fallback runway selection then skips any exclusive music orphaned
  with it and preserves the next ordinary playable segment. Once the dedication is
  already on air, however, its exact song owns the boundary even before queue
  admission. An admitted and ready song becomes the required survivor at the
  queue head. An admitted file that vanished before playback, or an active render
  fenced by the source/continuity revisions, restores the same token, track, and
  dedication id with fresh force/pin ownership for a retry under the new revision.
  Fresh or preserved fallback audio stays in the capacity-exempt slot so the
  retried song can enter the real queue first; the producer drain guard yields to
  this owner, while the slot and playback recovery ladder still cover a retry that
  misses the dedication boundary. In both cases the dedication finishes without
  a source-switch skip, and ownership
  remains until the promised song emits its first byte.
  Ordinary selection, norm-cache and
  last-known-good rescue, continuity reservation/slot claims, enqueue admission,
  and playback's final queue claim otherwise consult the shared cache-key plus
  canonical `(artist, title)` identity, so an unmarked copy cannot slip on air
  anonymously while the hosts still owe its dedication.
- † A priority insert also drops the segment at its **insertion point** outright (not just the
  furthest-future tail) when it carries a `transition_track_ref` — its "just
  finished playing" claim (baked into audio, crossfaded over the prior song's
  fade) is unconditionally broken the moment anything gets wedged ahead of it.
  Recorded as `GenerationWasteReason.STALE_PLAYED_TRACK_REF`; a fresh, accurate
  banter/ad-intro is produced on the next normal cycle.
- BANTER memory extraction is deliberately **not** a queue-time commit. The
  scriptwriter snapshots context, the producer rewrites that snapshot with the
  final aired lines including the transition, and the streamer schedules
  `memory_extractor` only after the send loop reaches EOF with bytes sent and at
  least one listener accepted a chunk. Purged, skipped, stale, failed, partial, or
  unheard banter never writes persona or song-cue memory.

### Protected continuity reservations

Program-replacing controls — source switches, playlist purges, panic, and
Chaos/Festival cutovers — rebuild the real playback queue and its Scaletta shadow
in one synchronous operation. They reserve only audio already safe to play:
eligible normalized-cache music first, then the packaged continuity clip when the
cache has nothing eligible, then the packaged `emergency_tone.mp3` when both are
unavailable. The clip is the rung below cached music, not a preamble in front of
it: a real song is both the better listener experience and the faster one to first
byte, because the cached payload takes its duration from the sidecar while the
clip needs an ffprobe first. A normalized-cache candidate passes the same final
blocklist rule as every other music admission, so a banned song cannot re-enter
through this instant-audio path, and the song currently on air (or one heard in
the last few segments) is skipped outright rather than reserved behind itself.

`force_next` is revision-owned rather than identified by its enum value alone.
Every semantic assignment, including a same-valued replacement or clear, advances
`StationState.force_next_revision`. A temporary owner clears through
`clear_force_next(expected_revision=..., expected_type=...)`, so stale cleanup
cannot retract a later control's force merely because both requested `MUSIC`.
Panic publishes its recovery force after the cut; abandoning an older listener
dedication therefore leaves that newer force intact. Listener pins and urgent
interrupt safety forces retain the revision they own, and Panic clears the
separate attribution for any older operator Air Next trigger it supersedes. An
urgent lifecycle remains owned after its force is claimed; a failed or stale
render is scheduled ahead of buffered and newer operator work, while Panic or
Stop explicitly settles its directive, bridge, and Moment Receipt.

Cache selection here shares the same rescue-rotation cooldown as the producer and
playback-gap rescues (`audio/norm_cache.py`): a cached song that aired as a rescue
within the last hour is deferred in favour of a fresher track, so repeated
controls do not keep reserving the same song. When every cached candidate is
still cooling, the reservation books the least-recently-heard one rather than
dropping to the emergency tone — real music always beats a tone. The cooldown is
fed only when a rescue is actually heard by a listener and resets on restart.

A successful replacement control supersedes an earlier reservation: it clears
ordinary and protected queued audio, clears any out-of-band `continuity_slot`,
and creates a fresh reservation for the new action. The one stronger owner is an
exact song promised by a dedication already on air. A ready admitted song stays
at the queue head; a vanished admitted file or active in-flight handoff is
restored for a revision-clean retry, with fresh continuity held out of band so
the real queue remains open. The resulting queue and shadow projection therefore
describe exactly the same final order. If no fresh reservation can be built, the
control fails closed instead:
it keeps the first immediately playable queued segment and any valid
capacity-exempt slot, drops only the remaining queued work to reopen producer
capacity, and never cuts the current segment into an empty runway. A companionship
cue counts as immediately
playable only while its listener-session epoch is current and its lifecycle state
is `QUEUED`, matching the playback fence that runs before any bytes reach air.
Every rebuild that drops queued work
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

### Stop/Resume transaction and playback truth

The admin Stop/Resume pair treats `cache_dir/session_stopped.flag` as the durable
session authority. `POST /api/stop` writes that marker before changing live state.
If the write fails, it returns `503` and leaves playback, queue, and reservations
untouched. After persistence succeeds, Stop marks the session stopped, clears the
listener-audible flag, advances `continuity_epoch`, cuts real media, purges queued
work, and clears interrupt, force, and continuity reservations. Cleanup after that
commit is best-effort: it may warn, but it cannot roll a durable stop back into a
half-running session. Startup restores the stopped state from the same marker.

Resume is the inverse, with audio readiness before state publication. While the
session is still stopped it reserves immediately playable runway: eligible
norm-cache music first, the manifested `continuity_1.mp3` clip on a cold cache,
then the manifested two-second `emergency_tone.mp3` last rung. If no readable
runway exists, normal Resume returns `503` with `force_available: true` and
keeps the marker. The admin may then explicitly confirm Force Start, which
removes the marker before touching live state, clears `session_stopped`, sets
`force_next=BANTER`, and wakes recovery without pretending that runway exists.
`/readyz` remains `503 starting` for that forced rebuild until a listener
actually accepts audio. A marker-removal failure leaves the session stopped and
all live fields untouched. With readable runway, normal Resume removes the
marker, clears `session_stopped`, and wakes producer/playback immediately. A
stream connection never clears the marker; only explicit Resume does.

Skip publishes a `skipping` transport sentinel before any best-effort history
write, so a second Skip is rejected while the first cut is still landing.
Listener history is settled exactly once: an audible music cut is recorded as
skipped and relinquishes its audible snapshot, while a selected-but-unheard
segment leaves the preceding heard track eligible for its eventual completion.
Panic may supersede an in-flight Skip when protected runway is ready; it clears
the audible snapshot only when the cut commits, and preserves it when the cut is
withheld so current audio can finish honestly.

`continuity_epoch` is the stale-work fence across this transaction. Stop always
advances it before queue cleanup. A Resume reservation uses the same continuity
rebuild path and advances it whenever that path mutates the queue or protected
slot. Producer renders, continuity bridges, startup prewarm, and playback
selections compare their captured epoch again at admission, so audio prepared
for the pre-Stop state cannot leak into the resumed timeline. Playback-built gap
fills bind themselves to the current epoch after their bounded asset probe; if
a later control still rejects one, the queue-gap clock and ladder position keep
running because rejected rescue bytes did not end the silence episode.

The fence has one deliberate exemption, and it is the subtlest part of this
design. A reservation advances `continuity_epoch` as a side effect of publishing
itself, so a naive fence would discard the very audio the control just reserved.
The problem is an ABA cycle: a playback task can already be blocked in
`queue.get()` from before a Stop, holding a local epoch that is necessarily
stale, while the runway queued after that Stop is new and must survive. Reserved
protected segments therefore carry a `continuity_admission_epoch` stamp
(`_stamp_continuity_runway_epoch`), and the playback loop admits a segment whose
stamp matches the current epoch even when its own captured epoch does not. The
exemption is narrow on purpose: only segments already carrying the reservation
flag are stamped, so ordinary pre-Stop work stays fenced.

Stamping is not optional for a caller, which is why it lives inside the
reservation helper rather than at each of the ~22 control sites. Anything that
advances the epoch owns keeping its own survivors admissible: `Resume` and
`Skip` trim a dead queue head before testing runway (`_discard_unplayable_queue_prefix`),
and that trim advances the epoch and re-stamps in the same call. An assetless
Panic, which reserves nothing and bumps the epoch itself, re-stamps at its own
site. Remove any one of those re-stamps and the failure is silent and specific:
the route answers 200, the operator sees success, and the reserved audio is
discarded as `stale_continuity` a moment later, turning a control meant to end
silence into one that extends it. Two of those paths are guarded by dedicated
tests for exactly that reason.

Because that trim runs on the Resume path, pressing **Start** on a station with
an evicted queue head files `operator_stop` rows in
`runtime_status.generation_waste.by_reason`. That is the dead head being cleared,
not a second Stop.

Playback has two truth boundaries:

1. **Selected/readable:** after the file opens and yields a non-empty chunk,
   `on_stream_segment_selected()` updates `now_streaming` and `playback_epoch`.
   This proves a readable selection, not listener delivery.
2. **Listener-audible:** only after at least one listener queue accepts that
   chunk does `on_stream_segment_audible()` set `current_stream_audible` and
   commit provider state, rescue rotation, continuity-fire receipts, and other
   heard-only bookkeeping. `runtime_status.station_on_air` is established at
   this second boundary and stays stable for a three-second segment-handoff
   grace only while a listener remains connected; a stopped session, expired
   handoff, task failure, or silence alarm still clears it. A `now_streaming`
   row by itself must never be cited as proof that a listener heard audio.

Provider status follows the same truth split. The main provider row describes
the last listener-audible route, while a newer unheard route is exposed
separately as `latest_observation` so an operator can see a current
action-required failure without the UI claiming that provider is on air.
Operator-facing script/TTS reasons are humanized; the corresponding recent
event retains its raw code as `diagnostic_reason`. Each producer render binds
an internal task-local observation token around script and TTS work. Only
observations owned by that token move onto the resulting segment, including the
aggregate TTS route and each `tts:<engine>` route; an independently scheduled
post-air memory call cannot be attributed to a coincident render. Failed,
cancelled, and rescue renders drain their token-owned observations, and the
token itself is never part of a public status or integration payload.

The required recovery set is a subset, not an exact directory inventory:
additional reviewed assets may ship. Release checks independently require
`continuity_1.mp3` and `emergency_tone.mp3` to exist, exceed 1 KiB, resolve as
package/image resources, match `assets/demo/spoken_assets.json`, and expose an
audio stream to `ffprobe`; validation must inspect both even when the first is
valid.

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
Before queueing, `mammamiradio/audio/imaging.py` may add transition stings at
music/speech boundaries or mix an electronic scene recipe around ad dialogue.
It also mixes identity stings under sweepers. Modern Night Drive is the default
root, and a custom root can replace it. Generated stings and beds provide the
legacy fallback and reuse matching `synth_` cache renders.

Bounded state lists (`played_tracks`, `running_jokes`, `segment_log`, `stream_log`, `ad_history`, `recent_outcomes`) use `deque(maxlen=N)` for automatic memory management — no manual truncation needed.

**Carosello session experiment.** During an ad break, the listener shows its
fictional brands in source order. It also reports process-local brand counts. A
non-fallback break receives credit only after EOF when it sent audio and every
emitted chunk reached at least one listener queue. `StationState` keeps the
counts in memory, so they reset on restart and add no playback-path I/O.
`/public-status.ad_experiment` exposes the payload, which `/status` reuses.
These counts are experimental and unsuitable for advertiser analytics.

**Callback Director (cross-domain verbal gags).** A gag planted in DJ banter can resurface once inside an unrelated news flash or ad — a rare, cross-domain "callback". `hosts/verbal_gag_ledger.py` (`VerbalGagLedger`, in-memory, session-ephemeral) holds banter-seeded gags and reuses `home/gag_select.py`'s `weighted_offer` (the same weighted-pick + 0.55 silence roll that `home/evening_memory.py`'s `EveningLedger` uses for HA-event gags). Lifecycle, all at QUEUE time so a discarded segment never plants or burns a gag: banter's `new_joke {text, punch}` is stashed on `state.pending_verbal_gag` and committed to the ledger in the banter success callback; before a flash/ad the producer calls `offer(contrasting_to=...)` and passes at most one gag to the scriptwriter (which injects a "land this here" instruction, or omits the key entirely); the gag is hard-retired after one travel, and only when the generator reports it actually landed (`callback_used`). Durable listener persona and song-cue extraction are a separate post-air path, so queue-time gag bookkeeping can still happen without treating unheard banter as long-term memory. Flash/ad prompts no longer carry the full `running_jokes` list — `running_jokes` stays banter's self-reference + persona-store store.

**Evening running gags (HA-event callbacks).** `home/evening_memory.py`'s `EveningLedger` tallies repeated discrete home toggles across an evening and surfaces a deferred, approximate callback ("the coffee machine, on again tonight") into banter via the STASERA prompt block. Gag-candidacy is decided by device **domain** (not hardcoded entity_ids), so it works on any operator's home out of the box: `switch`/`fan`/`lock`/`vacuum`/`binary_sensor` toggles are gag-worthy, while `sensor`/`climate`/`media_player`/`weather`/`light` and `person.*` are not. Operators tune this via `[home.running_gags]` in `radio.toml` (`domain_allowlist` replaces the default domain set; `entity_allowlist` restricts to specific entity_ids; `entity_denylist` silences chatty entities) — parsed into `core/config.EveningGagsSection`, degrade-to-default on malformed input. An evening "session" ends after `EVENING_GAP_SECONDS` (3.5h) with no real home activity — `last_active` advances only on real activity (excluding numeric drift, `person.*`, device-availability flaps, and passive `weather`/`sun` changes), so neither radio-cadence polling nor passive environmental events can keep a quiet evening alive forever — or at the 4am day rollover.

**Moment Receipts (the durable trail behind ritual-recipe moments).** `home/moment_receipts.py`'s `MomentStore` records every Home Assistant ritual-recipe moment from match through confirmed air, so a listener can verify a home-triggered reaction was real and an operator can answer "why did the host say that" (or "why did nothing happen"). One recording model covers all live delivery lanes, because they all air through the next banter segment: match rows are recorded at the producer poll site (`elected`, or `dropped` with a reason — `directive_slot_busy`, `interrupt_slot_busy`, `interrupt_cooldown`); the row's opaque id travels to the consuming segment's metadata (`ritual_moment_id`: a consumed directive's id rides the scriptwriter's consume/restore handoff — the banter result, not live state, so a fresh HA poll mid-generation can't cross the wires — while an interrupt directive deliberately keeps its id in the pending slot until queue-commit, protected because new matches drop as `*_slot_busy` while it waits; `gag_moment_id` for ritual-sourced evening-gag buckets, whose `ritual_family` provenance threads `HomeEvent → GagBucket` and upgrades the bucket's label to the generic family label so a device name can never become a receipt label); `StationState.on_stream_segment_audible()` flips the row to a provisional `airing` once at least one listener has accepted the segment's first chunk — not at selection or send-start, so a segment nobody could hear never claims a moment (rescue/fallback fills are guarded out too — backup audio never claims credit for the house); and the playback loop's finally records the true outcome verbatim from `classify_stream_outcome` (`aired`/`skipped`/`no_listeners`/`not_streamed`/`fallback_rescue`), independent of the provenance ledger. A moment whose path to air dies later is demoted with the same honesty: `generation_failed` (stock-copy fallback or a post-consume render death), `canned_fallback` (a canned clip aired instead of the gag), `interrupt_override` (a live cut-in clobbered the waiting directive), `muted` (operator muted the entity mid-flight), and `restart` (`load()` demotes stale `elected`/`airing` rows, since neither the pending directive nor the airing finalize survives a restart). Persistence mirrors the evening ledger: `cache_dir/moments.json`, atomic write, corrupt-tolerant load, `_CACHE_PROTECTED`, capped at 100 rows with 7-day retention — and the disk write happens only at the producer's save site; streamer paths mutate in memory and set the dirty flag, so the playback loop never does JSON I/O. Surfaces: `/public-status` exposes `ha_moments.recent` (≤3 rows, generic `public_family_label` + coarse age only — no entity ids, confidence, or spoken lines on the unauthenticated endpoint; an `airing` row shows only while its segment is what `now_streaming` plays); the admin `/status` exposes the full trail as `moments_admin` (≤25 rows) behind admin auth. Every store call is best-effort and never raises into the audio path.

Chaos Mode adds three state fields around the existing queue model: `chaos_mode_active`, a typed `chaos_pending` first-strike slot, and `chaos_cutover_epoch`. Enabling the mode purges pre-produced lookahead segments, bumps the epoch so any in-flight pre-chaos segment is discarded at commit, and queues a chaos-flavored `BANTER` next. Disabling clears `chaos_pending` and bumps the epoch without purging already queued audio. `played_track_log` is a separate play-time history used by impossible-recall chaos prompts; it is populated in `on_stream_segment_audible()` for music — once a listener has actually accepted the audio, not when music is merely queued or selected.

### Studio atmosphere

Two features create the illusion of a live radio studio:

- **Studio bleed**: After producing a music segment, the producer mixes a faint (-22dB) snippet of a previously-played banter clip under ~35% of music segments. This creates the "someone left a mic on" feeling.
- **Humanity events**: A one-shot event system (cough, paper rustle, chair creak, pen tap) fires exactly once per session after 15+ segments have been produced. SFX files live in `mammamiradio/assets/demo/sfx/studio/` (inside the package so `mammamiradio/scheduling/producer.py` and packaging find them together).

### Clip sharing

`POST /api/clip` can publish only one complete bundled starter track whose path, hash, identity, and attribution still match the canonical manifest. The playback loop records that snapshot only after a clean full-track send; the endpoint revalidates the package file before copying it into `{cache_dir}/clips/`. Jamendo, local, mixed, partial, unknown, ad, and banter windows fail closed with `403 music_share_unavailable`; none of their bytes can enter the public clip store. Eligible clips are served without auth at `GET /clips/{id}.mp3` and auto-expire after 24 hours. Per-IP rate limiting (1 clip per 10 seconds, rolled back when no eligible starter window exists) and a 50-clip disk cap prevent abuse. The listener maps the structured failure code to actionable copy. The one path by which host or ad audio becomes publicly servable is an operator-initiated keepsake (below), which writes to a different directory, is voice-only and music-tail-refused, and carries no TTL.

### Keepsakes

A clip expires after 24 hours and the provenance ledger prunes after `MAMMAMIRADIO_LEDGER_RETENTION_DAYS`, so a moment worth keeping is deleted twice over by default. `POST /api/clip/keep` (admin auth) exports the airing voice segment to `{cache_dir}/keepsakes/{id}.mp3` with a `{id}.json` sidecar, and nothing in the system expires it.

Durability is structural rather than a flag: `cleanup_old_clips` and the `CLIP_MAX_SAVED` cap operate on `clips/` alone, every cache pruner globs `cache_dir/*.mp3` without recursing, and `cleanup_old_clips` refuses outright when handed a directory named `keepsakes`. The add-on backup contract includes the directory, so a kept moment survives a restore.

**Two eligibility gates, both failing closed** (`is_keepsake_eligible` in `scheduling/clip.py`): the segment type must be in `KEEPSAKE_SEGMENT_TYPES` (banter, ad, news_flash, station_id, sweeper, time_check), and the segment must not carry `has_music_tail`. `commit_music_handoff` crossfades the outgoing song's real master under the opening seconds of the next voice segment, so type alone is not proof of provenance; a tailed segment is refused rather than trimmed, because a keepsake never expires and is served without auth.

**Provenance and byte ownership share one boundary.** The playback loop writes `app.state.clip_segment` (`{type, chunks, title, has_music_tail}`) at every segment boundary and counts the chunks that segment puts into the share ring; the route cuts exactly those chunks (`extract_segment_audio`) rather than deriving a byte length from wall-clock elapsed. The ring holds every voice segment back to back, since music never enters it, so a window one chunk too long would be the previous segment's audio labelled with this one's title and rights check. The same count sizes the `last_shareworthy_clip` lookback snapshot, written for all six keepable types, which lets an operator catch a break that has just ended; the lookback is consulted only when the airing segment is not a keepable type, so `too_early` and `music_tail` stay honest refusals about what is on air now.

Bounds: `KEEPSAKE_MAX_SAVED` (200) and a `KEEPSAKE_MIN_FREE_MB` (256) free-space preflight, run together with the file publication under `_keepsake_write_lock` so two presses cannot both pass at the ceiling. At roughly 4 MB for a long segment a full shelf is about 800 MB on the same volume as the norm cache, and no evictor reclaims it. The write is `mkstemp` + `fsync` + `os.replace`, so a kill mid-write can never publish a truncated keepsake. The scratch such a kill leaves behind is swept at the next startup by `prune_stale_keepsake_tmp_files`, since nothing else in the cache recurses into this directory.

Keepsakes reuse the `/clips/{id}.mp3` and `/clips/{id}` URLs and are checked after `clips/`, so an id can never resolve to a keepsake while a live clip of that id exists. `GET /api/clip/keep` and `DELETE /api/clip/keep/{id}` list and revoke them, surfaced as **Kept moments** in the admin Archivio tab; deleting the file is the whole revocation, since both public routes read from disk per request.

### Optional standalone chart refresh

When a standalone operator has deliberately enabled `external-media` and selected
charts as the base, the producer may check every 90 minutes and merge new chart
entries without resetting `played_tracks` history. This path is absent from both
add-ons and never substitutes for the offline starter release floor.

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
`run_playback_loop`) keeps a **4-second send-ahead target** on one monotonic
source media timeline. At 192 kbps that is roughly the first 32 packets; after
that, the schedule stays ahead by the target plus at most one further packet —
4.125 s in total. This absorbs an event-loop or CPU scheduling pause — including
one caused by rendering a newly created station ID, ad, banter, or a Home
Assistant projection — before it reaches a direct listener (for example, a Sonos
player consuming `/stream`). The worst such pause measured on HA Green was
1.781 s. At 192 kbps the cushion occupies 32 of every listener queue's 128
packet slots, so a slow listener has roughly 12 s of stall budget before it is
dropped rather than the ~16 s the queue holds.

The timeline is deliberately **continuous across natural segment boundaries**:
music → station ID → ad → banter → music share one origin, so the lead is not
re-accrued at each transition (which would add silence and drift). The pacer
resets only on a true discontinuity — no listeners (including a subsequent
mid-segment room refill), playback stop/resume, a real queue gap / fallback, or
an explicit skip — via a named `reset_timeline(reason)` call.

If a pause is longer than the whole lead, the pacer uses **at most a three-packet
recovery phase**, then rebases the pacing origin once and records the deficit as
an `overrun_rebased` event. At the default packet cap, that phase restores 375
ms; ordinary bounded packets may follow immediately until the 4-second target is
rebuilt. It never sleeps a negative interval and never turns the missed
wall-clock history into an unbounded backlog of overdue chunks — the unavoidable
long stall stays audible, but it cannot compound into a second catch-up phase or
many seconds of stale playback. The packet cap changes source-read granularity
while leaving bitrate, ICY metadata, queue ordering, and overflow protection
intact. Because listener queues remain bounded by packets, their shorter packets
give a slow listener a tighter time budget before drop. The 4 s / 4.125 s bound
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
normalizer produces, the starter-media gate verifies, and the `/stream` response
headers advertise. Packaged speech/recovery assets are not all guaranteed to be
re-encoded to this format, so players must rely on MP3 frame self-description for
exact decode parameters on a per-frame basis. The contract `audio_format`
provides is the nominal one, which is the same contract every ICY-headered
internet radio publishes.

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

`fetch_startup_playlist()` (in `mammamiradio/playlist/playlist.py`) chooses one
durable base; Jamendo is deliberately outside this function:

1. **Eligible persisted base.** A prior local or effectively enabled standalone
   external selection may restore. A legacy `jamendo://` selection is retired and
   rewritten to the current base. Both add-ons reject any persisted selection that
   would require extractor authority.
2. **Operator local files.** Supported audio under configured local roots becomes the base when present. In the Home Assistant add-on the primary root is `/media/mammamiradio`; legacy `/data/music` and standalone `./music` (or `MAMMAMIRADIO_MUSIC_DIR`) remain discoverable. `mammamiradio/playlist/local_library.py` owns recursive discovery, reconciliation, and the 60-second background scan; local tracks overlay an active charts/Jamendo/starter base without switching the selected source. They receive no project license claim; the operator owns their provenance and permitted use.
3. **Bundled starter catalog.** With no local base, runtime loads the twelve
   hash-pinned attribution-only derivatives from the canonical manifest
   (Incompetech under CC BY 4.0, Jamendo under CC BY 3.0).
   They play directly without normalization and complete one full cycle before a
   starter repeat. A release fails unless the exact 12 tracks, at least 45 minutes,
   complete human audition evidence, and no more than 75 MiB pass media proof.

Two optional expansions sit outside that base:

- **Jamendo transient provider.** Explicitly off by default; enabling requires an
  operator client ID and the current non-commercial-use acknowledgement while
  provider confirmation remains pending. It reads the API's streaming `audio`
  field with `audioformat=mp32`, admits only validated CC BY 3.0/4.0 candidates,
  and owns one in-memory lease plus one normalized partial/final artifact total.
  At most one prepared track can follow every two local/starter tracks. Bytes and
  lease metadata never enter cache, SQLite, rescue, handoff, clips, derivatives,
  or restart state, and the artifact is deleted after play or cancellation.
- **Standalone `external-media`.** The optional package extra supplies `yt-dlp`
  for operator-enabled charts, classic eras, and other supported sites. Technical
  access is reported as `operator_enabled`, not cleared. Stable and Edge add-on
  images omit the distribution, module, and executable and ignore legacy
  enablement; external-only actions return actionable `403` responses.

The admin Rotazione panel shows a persistent ready starter/local row and the
five-state Jamendo row; it does not present Jamendo as a playlist import. `/status`
returns a bounded playlist window plus redacted Jamendo operational facts. Public
status exposes only safe now-playing attribution. See
[Music sources and rights boundaries](music-sources.md) for the normative
provider, attribution, and release contract.

Once playback is running, protected recovery uses only admitted non-transient
media, packaged recovery audio, the emergency tone, or forced banter. Jamendo is
never eligible for rescue, and silent audio is never queued intentionally.

### Operator song blocklist

Base sources are reconstructed on startup, so an in-memory "remove" could
otherwise reappear. The operator blocklist makes a ban durable. It persists to
`cache_dir/blocklist.json` as `{serialized_key: {display, banned_by, banned_at}}`,
keyed by `normalized_track_key(track) = (artist.strip().lower(),
title.strip().lower())` (the same stored key used for playlist dedup, so a ban
holds across sources). Hard blocklist checks compare those stored keys with exact
normalized equivalence: accents, intra-word punctuation, compact artist spacing,
explicit platform uploader wrappers, and conventional primary-artist feature
credits cannot make the same recording reappear under a cosmetically different
key. The store is best-effort and corrupt-tolerant: a missing or malformed file
loads as empty and never raises into the audio path; writes are atomic (`tmp` +
`os.replace`).

Chart refresh and external/listener download doorways mentioned below exist only
in an effective standalone `external-media` process. Add-ons retain local search,
turn listener music requests into shout-outs, and never enter those commits.

Listener search results acquire that station identity from verified candidate
metadata, never from the listener's wording. An artist-field feature credit, or a
title credit that is bracketed, terminal separator-led, or a conservative
punctuated abbreviation, collapses before `Track` creation to the same base
artist/title. Ambiguous literal or unpunctuated `feat`/`ft`/`featuring` wording
remains part of the title unless a separately named guest corroborates the
candidate's lowercase credit interpretation; compound separator tails remain
literal. The full candidate credit and verified guest identity remain
admission-time blocklist aliases; a guest tail stays whole so a band name such as
`Earth, Wind & Fire` is never guessed to be three individual performers.

`core.song_identity.song_identity_key_is_blocklisted` is the shared hard-policy comparison. `playlist.filter_blocklisted` wraps it at every doorway where tracks enter `state.playlist`: startup (`main.py`), source switch (`_apply_loaded_source`), the mid-session chart refresh (`fetch_chart_refresh`), and bulk source loads. External/listener download commit, restart handoff, producer admission, continuity selection, playback's last-mile fence, and norm-cache **rescue** call the same comparison directly because they can serve audio without passing through `state.playlist`; a banned song therefore cannot re-enter through cached, queued, or post-restart audio under an equivalent spelling. The external/listener commit returns a distinct `"banned"` status (not `"dropped"`): the admin gets an honest "it's banned" notice and a listener request fails loudly (`song_error`) instead of spinning on "searching…". Bulk `/api/playlist/enrich` honors the blocklist; only an explicit single `/api/playlist/add` bypasses it as an intentional override. Banning (`POST /api/track/ban`, or the per-row `/api/playlist/remove`) also clears a matching `pinned_track` and synchronously drops any not-yet-started queued segment of the song — the currently-airing segment finishes untouched, so a ban never causes dead air. Dropping a segment can expose a different queue tail; `_apply_ban` and manual `/api/queue/remove` both re-verify that newly exposed tail (`_reconcile_queue_tail_adjacency`) rather than trust it blindly, since only rescue/recycled music can safely re-anchor speech-bed adjacency — ordinary rendered music may carry an egress-processed path. A last-mile fence in the playback loop itself covers the remaining race, where a banned track was already pulled off the queue before a ban's synchronous purge reached it: playback discards that segment immediately, before any bytes reach air, and runs the same tail-adjacency reconciliation. That same playback-owned case leaves a listener-request token behind: a promised file already claimed by playback is out of the queue, so `drop_matching_segments` never releases its `listener_request_admitted_reservations` entry. `_apply_ban` therefore filters that map with the same canonical predicate as its last step — after the purge, so a still-queued dedication and its linked song settle through the normal paired mutation first. Without it, a pre-first-byte failure would call `restore_listener_request_handoff_before_first_byte` and re-arm the banned promise as a retry *after* the retry filter ran (a stuck exclusive promise the producer can only answer with `BLOCKLIST_GATE`), and `listener_track_reservations()` would keep suppressing cache and recovery audio for a recording that may never air again. Recovery paths carry a matching guard one level up: error recovery, the quality-gate circuit breaker's last-known-good recycling, and speech-bed adjacency selection all resolve their candidate through `_blocklist_safe_last_music`, which requires a durable `{artist, title}` identity and rejects it outright — even when unidentified — while any ban is active, so none of those paths can reintroduce an operator-banned song through a cached or adjacency-based route. The one path that **does** interrupt the airing song is the on-air console's **Ban** button (`POST /api/track/ban-now-playing`): it resolves identity from `now_streaming.metadata` (`artist`/`title_only`, falling back to parsing the `Artist — Title` label, so it bans even a rescue-cache or one-off song that never entered `state.playlist`), runs `_apply_ban` to purge queued copies, then reuses the exact skip path (`_request_skip`: listener-skip record, a bridge to forced music whenever no immediately playable runway remains — not just an empty queue, `skip_event`, `now_streaming → skipping`). Ban precedes skip so the bridge sees the post-purge, playback-verified runway state and still force-bridges to music if nothing left in the queue can actually play — never dead air. It is starvation-exempt like the per-row ✕ Ban. A bulk ban that would leave fewer than `MIN_ROTATION_AFTER_BAN` songs (or that would empty an already-small pool) is refused with a warm message rather than starving the pool onto the rescue path; a single per-row removal stays exempt. The persist call is best-effort — when `blocklist.json` can't be written the ban still holds for the session and the API echoes `persisted: false` so the admin UI says "banned for now, may come back after a restart" instead of promising permanence. `POST /api/track/unban` and `GET /api/track/banlist` back the admin "Banned" manager. Listener thumbs-down voting is a separate later slice; this layer is operator-only.

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

**ElevenLabs TTS**: requires `ELEVENLABS_API_KEY` and operator-provided voice IDs. V2 (`eleven_multilingual_v2`) is the default for ads, sweepers, guest bits, and every host. The expressive `eleven_v3` delivery path (with a code-owned `delivery_profile`) is present in the code but disabled by default: Marco and Giulia currently ship on V2 after their V3 host-performance audition was rejected. When a host opts into `eleven_v3`, V3 accepts only `stability`, never V2-only similarity, style, or Speaker Boost controls.

For selected normal host banter on a V3 host, the script carries one semantic
cue beside — never inside — the clean spoken text. Marco may be `energetic`, `curious`, or
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

Playback verifies the stamped epoch before the segment and before every audio chunk. A mismatch is discarded through `GenerationWasteReason.LISTENER_SESSION_STALE` before that chunk reaches the hub. `LiveStreamHub.broadcast()` reports how many listener queues accepted the chunk; only the first positive acceptance moves the cue to `CONSUMED` and commits listener-audible state. File selection may briefly create provisional now-playing metadata, but a rejected or stale companionship cue clears that selection before status readers can keep advertising unheard audio as live. The central `StationState.record_discard()` boundary owns abandonment for all unstarted queue cleanup, and the queue shadow verifies the pulled `queue_id` before removing a row, rebuilding from the real bounded queue if the projection ever drifts.

Hosts may build shared station mythology, but may not turn a stream connection into an arrival, return, or identity claim. The final producer boundary checks the assembled transition plus banter text in English and Italian; it makes one bounded identity-free repair attempt and falls back to deterministic safe copy if the repair remains unsafe. A separately authorized, named Home Assistant resident-return fact is line-bound to its source entity; a door unlock or generic presence signal never grants that authority.

The hot `write_banter` contract does not write persona memory. Instead, `scriptwriter.py` creates a `MemoryExtractionCommit` snapshot, `producer.py` replaces its draft lines with the final aired script, and `streamer.py` schedules `hosts/memory_extractor.py` only after the banter segment finishes sending cleanly to at least one listener that accepted a chunk. The extractor then asks the fast script model for bounded `persona_updates` and applies them under a write lock. This post-air fast-lane call is automatic whenever generated banter has station-memory metadata and airs cleanly; there is no separate per-call opt-out beyond disabling the persona store or removing script-provider credentials.

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
- **temperature normalization.** `home/temperature.py` is the single authority for turning a Home Assistant temperature into Celsius before it reaches a host prompt, the news flash, the Casa card, or the narrow privacy projection. `temperature_unit_of()` reads HA's two conventions (`temperature_unit` for `weather.*`, `unit_of_measurement` for `sensor.*`); `normalize_temperature()` converts °C/°F/K and returns `None` for anything else; `format_celsius()` rounds to one decimal so a converted value is speakable (70 °F airs as `21.1°C`, never `21.111111111111114`) and renders empty for a non-finite value so `inf`/`nan` can never be spoken. `is_plausible_celsius()` filters *physical nonsense only* (−273.15…1000 °C): a Pi's own `sensor.processor_temperature` at 78 °C and a boiler flow at 85 °C are legitimately `device_class: temperature`, and withholding them would delete the entity from `context.scored` entirely, not just drop the number. The narrower "is this a room" judgement stays with the subtractive authorities (`context_director` −90…70, `authorization` −80…60).
- **the missing-unit policy.** `weather.*` publishes `temperature_unit` and `sensor.*` publishes `unit_of_measurement`, so every path reading one of those requires it: `require_unit=True` on the weather state line, both weather arcs, classified temperature sensors (in **both** `_format_state` and `DirectorObservation.from_home_assistant_state`, so the boundary is self-enforcing rather than relying on one module to filter for the other), and `authorization._temperature_c`. A unit is only ever defaulted to Celsius when it is absent or blank; an explicitly supplied non-string is malformed input and fails closed. **Fallback path:** an unresolvable unit withholds the number rather than assuming Celsius — a weather line keeps its condition, a climate line keeps its mode, and a unitless temperature sensor is dropped entirely. `climate.*` is the single genuine exception and the known gap: HA pre-converts climate values into the household's configured unit and publishes no unit attribute at all, so those keep the legacy Celsius assumption. Pinned by `test_format_state_climate_without_a_unit_attribute_reads_as_celsius` and stated plainly in the release notes rather than implied fixed.
- **forecast unit lookup.** `weather/get_forecasts` carries no unit, so `fetch_weather_forecast` issues a concurrent `GET /api/states/weather.forecast_home` (`asyncio.gather(..., return_exceptions=True)`, bounded by `_WEATHER_UNIT_TIMEOUT`, well under the 5s optional-enrichment budget) and prefers any inline unit over it. A `CancelledError` from that enrichment deadline is re-raised rather than downgraded, so a cancelled fetch never goes on to mutate the cache globals. If the unit cannot be resolved the arc still airs its condition but without a temperature. **Cache TTL:** `_WEATHER_DEGRADED_CACHE_TTL` (5 min) instead of the full `_WEATHER_CACHE_TTL` (1 h) whenever a retry could plausibly recover the number — an unreadable unit, or any transient failure — so one blip costs one poll rather than an hour of weather. An *empty* forecast keeps the full hour: that is a stable property of the integration, and retrying it every 5 minutes would be a permanent double-request treadmill on the Pi. Losing the temperature logs one WARNING on transition (not per poll), because a mis-scoped token would otherwise strip it from every break forever with nothing above DEBUG to explain why.
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
parent process's event loop. Once the raw response bytes and enrichment values
are available, JSON decoding plus the pure projection run through one
module-owned `ProcessPoolExecutor(max_workers=1)`, created lazily at the first
projection and configured with the multiprocessing `spawn` context. This keeps
projection CPU work out of the process that paces `run_playback_loop` without
forking the multithreaded server. The worker process receives copied, inert
request values plus the cache-directory path, reads its own detached
label-catalog snapshot, and returns only a candidate
(`_HomeContextProjectionCandidate`). It never touches `StationState`, module
caches, persistence callbacks, event baselines, or any logging that contains HA
values.

The coordinator (`_HAContextRefreshCoordinator` in `producer.py`) stays the sole
owner of request lifetime, stage state, mute/authorization revalidation on the
parent loop, stale-result discard, observed-entity bookkeeping, and safe-boundary
adoption at `_drain_completed_result`. The parent coordinator remains the only
publication owner. A cancelled, timed-out (30 s total cap), closed, or superseded
request's worker value is ignored — it can never publish after coordinator close
or after a newer request, and no extra refresh begins while the retained request
still owns the mailbox. The single worker process serializes an abandoned
calculation and the next one; they never run concurrently.

The worker starts under `_init_ha_projection_worker`, which enforces two
properties the thread version got for free. It **drops every credential-shaped
environment variable** (any name ending `_KEY`, `_TOKEN`, `_SECRET`, or
`_PASSWORD`): spawning re-imports this module in the child, which re-runs
`core/config.py`'s module-scope `load_dotenv()`, so an unscrubbed worker would
hold every provider key while needing none — nothing in the projection's call
graph reads the environment at all. It then attaches a `NullHandler` to the
`mammamiradio` logger tree with `propagate = False`, because the worker never
runs the station's logging setup and a stray `WARNING`+ would otherwise skip
`LOG_LEVEL` and land raw in the add-on log. The projection is silent today; this
keeps that true by construction rather than by review.

To keep the worker cheap to start, `core/config.py` reads its two voice-validation
symbols straight from the `audio/voice_catalog.py` leaf instead of the aliases
re-exported by `audio/tts.py`. `tts` pulls in `openai`, `edge_tts`, and `aiohttp`,
and config sits in the worker's import graph; routing around it takes the child's
cold start from 1104 modules to 334 and roughly 0.35 s to 0.07 s. That cold start
is paid inside the first refresh after a restart, so it lands in the same
foreground budget as the cold label/weather warm-up.

Two failure modes are handled distinctly, because both would otherwise reach the
operator as the same generic `Failed to fetch HA context` line. If the worker
exits and breaks its process pool, that refresh is not retried in place: the
broken pool is retired (with its own log line, emitted only by the attempt that
owns the teardown) and the next scheduled refresh lazily creates a fresh spawned
worker. If the worker cannot **come up** at all, the cause is named once per
outage and repeats stay quiet until a pool starts successfully again. That path
catches both shapes deliberately, because they are not the same exception and do
not arrive at the same place: CPython's `_check_system_limits` rejects missing or
undersized semaphore support during construction with a **`NotImplementedError`**
(not an `OSError`), and latches the verdict process-wide so the outage is
permanent; while a `spawn` context defers process creation to the first `submit`,
so an exhausted process table or out-of-memory kernel arrives as an `OSError`
from the submit instead, and retires the worker-less pool. Both paths follow the
existing failed-refresh contract: return the stale context filtered against live
mutes when one exists, or an empty context otherwise. Audio is never affected
either way.

Because the projection executes in a child process, `[tool.coverage.run]` in
`pyproject.toml` sets `concurrency = ["multiprocessing", "thread"]` and
`parallel = true`. Without them coverage stops at the process boundary and
silently reports the projection as unexecuted, which drops `home/ha_context.py`
below its floor in `.coverage-floors.json`. The autouse
`_reset_ha_projection_executor` fixture in `tests/conftest.py` retires the pool
around every test so the worker exits cleanly and flushes that data, and so one
poisoned pool cannot cascade into later tests.

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

- **Starter/local tracks** use any admitted track artwork already present, then
  fall back to the station identity; artwork never substitutes for source/license
  attribution.
- **Jamendo tracks** may expose only provider-reported public artwork metadata for
  the current single-use lease. The private stream URL and lease facts never enter
  artwork cache or public status.
- **Standalone external tracks** may read chart artwork from Apple RSS or carry a
  search thumbnail; `playlist/cover_art.py` can upgrade it through the iTunes
  Search API on `_commit_external_download`, off the event loop. Results are cached
  to `cache_dir/cover_art_cache.json` (hashed key; definitive misses cached with a
  TTL, transient failures never cached). This entire download path is absent from
  both add-ons. Resolution is best-effort and a miss falls back to existing art or
  the station logo.

This is opportunistic context, not a hard dependency. Failures there should not stop the station.

### Timer interrupt flow

When a HA timer fires, the station interrupts playback with a pissed/urgent host
segment whenever a packaged bridge is available:

```text
HA timer fires (timer.xyz → idle, with recent finished_at)
    ↓
ha_context.py: lightweight 5s poll detects idle transition (separate from the default 300s full-state prompt-context fetch).
    Cancel/reset filter: only fire when finished_at is set and within the last 30s.
    ↓
check_reactive_triggers() → InterruptSpec(directive, urgency, cooldown)
    ↓
producer.py: _fire_interrupt(state, spec, queue, skip_event)
  1. Commit assets/sfx/alert.mp3, or the approved packaged emergency tone,
     to state.interrupt_slot. If neither exists, abort before draining or skipping.
  2. Drain lookahead queue and clear stale continuity/music adjacency.
  3. Demote any directive receipt being superseded, then store spec.directive.
  4. state.chaos_pending = ChaosSubtype.URGENT_INTERRUPT  (pissed tone)
  5. Clear superseded operator Air Next attribution and set a revision-owned
     BANTER force as the urgent safety belt.
  6. state.chaos_cutover_epoch += 1; skip_event.set() cuts the current segment.
    ↓
run_playback_loop: interrupt_slot checked before queue.get() → bridge plays (≤2s)
    ↓
Producer generates URGENT_INTERRUPT banter with directive (async, LLM)
    ↓
Pissed banter plays after bridge
```

Panic Cut and Stop are stronger controls than a pending urgent interrupt. They
guard-clear only the urgent force revision, clear its directive and bridge slot,
remove any ephemeral bridge, advance the cutover epoch, and mark its Moment
Receipt dropped. That ownership check never mistakes a newer force for the
urgent one; Panic then publishes recovery `MUSIC`, while Stop clears all forces.

Timer interrupts are configured via `[[homeassistant.timer_interrupt]]` blocks in `radio.toml`. The dedicated timer poll reads those entity IDs without mutating the module-level HA entity lists.

The same mechanism is callable directly via `POST /api/interrupt` (admin auth, 60s cooldown) — any HA automation can inject a custom directive without `radio.toml` configuration.

## Access model

### Listener song-request resolution

`POST /api/listener-request` still accepts shoutouts and song requests without
waiting for a catalogue search or download. Its successful response additively
includes the opaque `public_token` and nullable `song_resolution`; shoutouts use
`null`, while song requests use the states below. Existing response fields and
the stored `song_found`, `song_error`, and lifecycle `status` fields remain
available for compatibility. When external downloads are available, a
detected song request begins with `song_resolution: "searching"`, which means
that lookup and, when a candidate matches, download and admission are still
pending. When song downloads are disabled, the request is still classified
honestly as a song request but returns the immediate terminal resolution
`"failed"` instead of advertising work the station cannot perform.

`GET /public-listener-requests/{public_token}` lets the submitting listener
follow that one request without exposing the admin `request_id`, internal error
details, or mutation capabilities. Its terminal song resolutions are
`"matched"`, `"not_matched"`, and `"failed"`. A match is reported only after
the requested identity is verified and the downloaded track is committed to
the playlist; it means the track is ready for station scheduling, not that it
has already aired. Public unsuccessful outcomes are deliberately coarse:
`"no_verified_match"`, `"not_playable"`, or
`"temporarily_unavailable"`. They give the listener a safe next step while
keeping provider and admission details private. A token remains queryable while
its request is pending. Once the request is archived, its receipt remains
available for 300 seconds and then returns `404`. Receipt responses are
`Cache-Control: no-store`, and the service worker excludes the route, so a
transient `searching` response cannot mask a later terminal result.

The bound on following a receipt is the listener page's, not the route's: a
pending request has no server-side deadline, so `searching` can be the honest
answer for as long as it sits in the queue. `listener.js` stamps each stored
receipt with a start time and stops tracking after ten minutes, backing off to
at most 30s on retryable answers (transport failure, non-`2xx`, unparseable or
unknown body) while keeping the responsive 3s cadence whenever the station still
answers `searching`. Hitting that deadline hands the request form back and says
tracking stopped **without** claiming the request is gone — it is still queued —
so the listener is not invited into an immediate duplicate. The separate
`404`/`410` answer means the record really has been archived and pruned, and
gets its own copy (`form_song_tracking_lost`) that says so.

### Route table

Write routes that consume request details use `mammamiradio.web.json_body.read_json_object`.
Empty, malformed, or top-level non-object bodies return `422` with
`{"ok": false, "error": "<human message>"}` before endpoint-specific validation runs.
Admin auth dependencies still run before body parsing on protected routes.
`Admin (active setup)` uses that admin boundary plus the stricter local/private
Host or genuine HA-ingress rule described under [CSRF protection](#csrf-protection).

| Route | Method | Access | Description |
| --- | --- | --- | --- |
| `/` | GET | Public | Listener page. Over trusted HA ingress the admin panel is served instead. |
| `/listen` | GET | Public | Alias of `/` for backwards compatibility |
| `/admin` | GET | Admin | Admin control room panel |
| `/dashboard` | GET | Admin | 301 redirect to `/admin` (legacy) |
| `/sw.js` | GET | Public | PWA service worker |
| `/static/{filename:path}` | GET | Public | PWA static assets (manifest, icons) |
| `/favicon.ico` | GET | Public | Browser default favicon path; serves the station icon SVG |
| `/stream` | GET | Public | Infinite MP3 stream; a fresh install without audible proof receives the packaged First Listen mini-show before joining the shared live hub. `?first_listen=1` additionally waits up to `FIRST_LISTEN_RESUME_WAIT_SECONDS` (8s) for an explicit `/api/resume` and returns an empty body if the station stays stopped |
| `/healthz` | GET | Public | Runtime-health probe with process uptime; prolonged silence with active listeners returns `503`, while an intentional Stop remains healthy |
| `/readyz` | GET | Public | Readiness probe with queue depth and explicit `ready`, `starting`, or `stopped` status; listener-accepted audio proves readiness even during startup grace, while a persisted operator stop returns `503 stopped` |
| `/public-status` | GET | Public | Current segment, recent log, the real queued segments only (`upcoming_mode` is `queued` when render-ready audio exists and `building` when no render-ready segment exists yet), process-local `ad_experiment` completion counts, `playback_actions.skip_would_bridge` (whether cutting the current segment right now would have to bridge to forced music — true whenever no immediately playable queued or reserved audio remains, which can diverge from `upcoming_mode` since a queued segment can be render-ready but not itself playable, e.g. banned or stale), and `stream.audio_format` (the canonical encoding contract — see "Stream audio format metadata" below) |
| `/status` | GET | Admin | Full admin JSON: queue depth, uptime, scripts, `consumption` (session AI cost estimate, unpriced-model flag, and fixed-key cost breakdown for host scripts, transitions, ads, post-air memory extraction, and TTS), anonymous `listener_session` diagnostics (epoch, phase, active duration, pending persona count, and companionship cue state), HA context, errors, `provider_health`, `runtime_status` (normalized provider state, session failover event history, `bridge_health` rescue-bridge telemetry, `rescue_rotation` cached-music cooldown telemetry, `producer_headroom` readiness, bounded `render_timings` diagnostics, and `continuity_slot` — the admin-only projection of any reserved capacity-exempt safety audio, `{label, duration_sec, audio_source, reservation_id}` or `null` — see operations.md), `production` (the live "In produzione" feed — `current` is the phase the producer is building right now, `recent` is a bounded trail of just-finished work; admin-only, never in `/public-status`), `current_track_preference`, `moments_admin` (Moment Receipts full trail, ≤25 rows — see "Moment Receipts"), and `playlist_page` (`{total, offset, limit, has_more, revision}`). Accepts `?playlist_offset=0&playlist_limit=80` (max 200) for lazy loading. |
| `/api/setup/status` | GET | Admin (active setup) | First-run setup status, detected run mode, station mode, canonical `guided_setup` stages, and `first_listen`, `source_readiness`, `speaker`, `verification`, and `privacy` projections |
| `/api/setup/recheck` | POST | Admin (active setup) | Re-run setup probes |
| `/api/setup/first-listen/players` | POST | Admin (active setup) | Discover compatible-looking Home Assistant `media_player` targets without starting playback |
| `/api/setup/first-listen/play` | POST | Admin (active setup) | Ask one selected player to start `media-source://mammamiradio/live` and record the accepted attempt |
| `/api/setup/first-listen/receipt/retry` | POST | Admin (active setup) | Persist the server-owned accepted attempt after a receipt failure; never sends another playback request |
| `/api/setup/first-listen/verify` | POST | Admin (active setup) | Record the operator's heard/not-yet result for the current accepted attempt |
| `/api/setup/first-listen/listener-confirm` | POST | Admin (active setup) | Record browser-local audible proof as a `listener_*` attempt; this is the route that completes First Listen |
| `/api/setup/home-context-preview` | POST | Admin (active setup) | Fetch a fresh detached, filtered Home context preview without publishing it into host scripts |
| `/api/setup/home-context-choice` | PATCH | Admin (active setup) | Apply the explicit Home-context choice and record completion of the privacy review; enabling requires a fresh preview |
| `/api/setup/provider-check` | POST | Admin (active setup) | Active, secret-safe Anthropic/OpenAI/Azure Speech/ElevenLabs connectivity check |
| `/api/setup/addon-snippet` | GET | Admin | Copy-friendly Home Assistant add-on config snippet |
| `/api/homeassistant/context-candidates` | GET | Admin | Sanitized Home Assistant context preview for onboarding; includes additive `entities` rows while preserving legacy arrays, and is never exposed on `/public-status` |
| `/api/homeassistant/entity-policy` | PATCH | Admin (active setup) | Apply exactly one idempotent `muted` or `personal_moment_enabled` property to one Home Assistant entity; the response includes effective consent, policy revision, and the count of matching queued host breaks removed by a mute or a personal-moment consent revocation |
| `/api/shuffle` | POST | Admin | Shuffle playlist |
| `/api/skip` | POST | Admin | Skip current segment. Requires audible media: the loop parks with no listeners and leaves the finished segment's metadata in place at EOF, so selected-but-inaudible means there is nothing to cut. A skip already in flight says so instead of claiming nothing is streaming |
| `/api/panic` | POST | Admin | Emergency cut: reserve safe continuity, invalidate in-flight work, skip only when playable runway exists, and force the next segment to music without stopping the session |
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
| `/api/pacing` | PATCH | Admin | Patch pacing fields (songs between banter, ad spots per break, etc.); malformed bodies return 422, values are clamped to safe floors/ceilings, and HA add-on saves commit through Supervisor before live mutation |
| `/api/setup/save-keys` | POST | Admin (active setup) | Save API keys via dashboard |
| `/api/capabilities` | GET | Admin | Capability flags, tier, next-step hint, connect status, and provider degradation telemetry |
| `/api/chaos` | GET | Admin | Return `{"enabled": bool}` for Chaos Mode |
| `/api/chaos` | POST | Admin | Toggle Chaos Mode with `{"enabled": bool}`; persists `chaos_mode_active` to `.env` or Supervisor's stored HA add-on options |
| `/api/party` | GET | Admin | Return `{"active": bool, "mode": str\|null}` for Festival Mode |
| `/api/party` | POST | Admin | Toggle Festival Mode with `{"action": "enable"\|"disable", "mode": "festival"}`; persists `festival_mode` to `.env` or Supervisor's stored HA add-on options; purges queue and arms first-strike banter on enable |
| `/api/quality` | GET | Admin | Return `{"active_profile": str, "profiles": [str]}` for the model quality dial |
| `/api/quality` | POST | Admin | Set the active model profile with `{"quality_profile": "premium"\|"balanced"\|"economy"}`; hot-swaps live with no restart and no queue purge; persists `MAMMAMIRADIO_QUALITY` to `.env` or `quality_profile` through Supervisor |
| `/api/trigger` | POST | Admin | Trigger segment production |
| `/api/stop` | POST | Admin | Persist the stop marker first, then invalidate stale work, cut, purge, and pause producer until `/api/resume`; persistence failure changes nothing |
| `/api/resume` | POST | Admin | With readable runway, clear the durable stop marker and return `{"ok":true,"recovering":false}`; without assets, remain stopped with `503` + `force_available:true`. Only an explicitly confirmed `?force=true` clears the marker without runway, arms recovery, and returns `{"ok":true,"recovering":true,"runway_source":"none"}` |
| `/api/credentials` | POST | Admin | Update credentials at runtime |
| `/api/clip` | POST | Public | Capture eligible material; music requires a complete bundled-starter-only window, otherwise `403 music_share_unavailable` |
| `/api/clip/keep` | POST | Admin | Keep the airing voice segment (or the one just ended) durably in `cache_dir/keepsakes/`. Refusal reasons: `music`, `music_tail`, `not_on_air`, `too_early`, `not_keepable`, `archive_full`, `no_room`, `write_failed` |
| `/api/clip/keep` | GET | Admin | List kept moments, newest first (`keepsake_id`, title, segment type, created_at, size, share URL) |
| `/api/clip/keep/{id}` | DELETE | Admin | Remove one kept moment, audio and sidecar together; the supported way to revoke audio that never expires |
| `/clips/{id}.mp3` | GET | Public | Serve a saved clip (no auth, for sharing). Falls back to `keepsakes/` when the clip is missing or expired, and keepsakes carry no TTL |
| `/clips/{id}` | GET | Public | Share landing page (OG card + player). Falls back to `keepsakes/` when the clip is missing or expired; an expired clip renders an "this moment has passed" state rather than a 404 |
| `/api/track-rules` | POST | Admin | Flag a reaction rule for the current track |
| `/api/listener-request` | POST | Public | Submit a song request or shoutout; successful responses add `public_token` and the current `song_resolution` for listener-side follow-up |
| `/public-listener-requests` | GET | Public | Sanitized listener-request feed for the on-page sidebar (`public_token`, `status`, `song_resolution`, name, message, type) — admin `request_id`, `submitter_ip_hash`, and `evict_after` stay server-side |
| `/public-listener-requests/{public_token}` | GET | Public | Safe resolution receipt for one submission: `null` for a shoutout or `searching`, `matched`, `not_matched`, or `failed` for a song request, with a cleaned track on matches or a coarse actionable outcome on failures |
| `/api/listener-requests` | GET | Admin | List pending listener requests (full record including `request_id`, `status`, `evict_after`) |
| `/api/listener-requests/dismiss` | POST | Admin | Dismiss a pending listener request by `ts` (legacy) or `request_id` (canonical); only queue admission may mark a request as sent to the hosts |
| `/api/playlist` | GET | Admin | Paginated playlist window; `?offset=0&limit=80` (max 200); returns `{tracks, total, offset, limit, has_more, revision}` with each admin track carrying an opaque row `id` and its current `preference` score |
| `/api/search` | GET | Admin | Always searches the local/base playlist; pagination uses `offset`/`limit` (max 50 local, max 10 external) and `external_offset`/`external_limit`. With standalone `external-media`, external results may be included and `include_external=false` skips yt-dlp after the client exhausts them; add-ons return no extractor results. Every response (including an empty query) returns the playlist `revision` captured with the local snapshot before any external lookup, and each local result carries its opaque row `id` |
| `/api/heading` | POST | Admin | Steer the next music stretch with an era seed (`{"seed": "classic://italian/80s"}`) or free text (`{"text": "2000s female vocals"}`), without purging the queue. Matching base tracks work everywhere; an era import requires standalone `external-media`, with add-ons failing external-only work closed |
| `/api/direction` | POST | Admin | Free-text alias for heading direction (`{"text": "sunday morning italian"}`) over matching base tracks; only standalone `external-media` may expand, search, and download new targets in the background |
| `/api/heading/clear` | POST | Admin | Clear the active heading/direction and return to automatic rotation without removing blended tracks |
| `/api/playlist/add-external` | POST | Admin | Standalone-only external add; add-ons return actionable `403 external_media_unavailable_in_addon` |
| `/api/media-sources/jamendo` | PUT | Admin | Retain/replace/clear the client ID and persist explicit enabled + non-commercial acknowledgement intent; returns redacted status |
| `/api/media-sources/jamendo/retry` | POST | Admin | Coalesce a transient-provider retry (`202` enabled; `409 jamendo_retry_disabled` when off) |
| `/api/media-sources/local/scan` | POST | Admin | Run the local-library scanner immediately; concurrent requests coalesce on the in-flight scan and return `in_progress` state |
| `/api/interrupt` | POST | Admin | Immediately interrupt the stream — hosts deliver pissed/urgent banter with a custom directive. Body: `{"directive": str, "urgency": "pissed"\|"urgent"\|"gentle"}`. 60s cooldown enforced; returns 429 on spam. |
| `/api/hot-reload` | POST | Admin | Reload `language_policy.py`, `prompt_world.py`, `relationship.py`, `transitions.py`, `fallbacks.py`, `station_name_guard.py`, then `scriptwriter.py` (leaves-first) in-place via `importlib.reload()` — stream continues uninterrupted, next banter uses new code. Requires `--workers 1`. `memory_extractor.py` is deliberately excluded — it holds live in-flight task/apply-lock state a reload would reset mid-extraction. |

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

First Listen, setup credential actions, speaker playback, and Home entity
privacy controls use an additional DNS-rebinding boundary. Their setup-status
read and active routes accept, checked in this order: `X-Radio-Admin-Token`
alone (automation; returns before the CSRF token is even read, since a
caller-supplied secret is not what CSRF defends); verified HTTP Basic admin
credentials plus the injected CSRF token (the supported browser path when
`ADMIN_PASSWORD` is configured, including on a custom hostname, because a
credential is a secret a rebound page cannot manufacture); or a literal
local/private IP Host or genuine HA ingress plus the CSRF token. A custom
hostname with no configured password and no admin token is refused with a
structured `403` (`{"code": "active_setup_host_untrusted", "title", "message",
"action"}`) that names the fix; a missing or stale CSRF token on the other two
paths gets the same shape under `active_setup_csrf_stale`. This stricter rule
is implemented by `_require_active_setup_access` and does not change the
legacy admin matrix for unrelated endpoints. `docs/operations.md` "Admin
access model" is the SSOT; the
two must change together.

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
uses a `source_revision` counter on `StationState` to detect and discard segments
generated for a stale source. `/api/shuffle` increments the broader
`playlist_revision`, but that alone no longer discards in-flight producer work:
reordering the pool does not make a rendered song unplayable, and the render is
kept and queued against the new sequence.

If a slow source request crosses a Stop or another continuity-epoch change, its
commit is metadata-only: the playlist metadata may change, but the request
preserves the real queue, queue shadow, and protected slots byte-for-byte. It
does not own transport work admitted after the epoch it captured. That is
enforced by *which method it calls*, not by restoring fields afterwards:
`switch_playlist` is split into `_apply_playlist_context` (the crate — records,
diversity history, cadence counters, steering) and the revocation half on top of
it (pin, force slot, pending actions, pending listener requests, the per-IP
request rate limiter, the active / admitted / retry listener-request handoff
stores, the starter cycle, and music admission reservations). A metadata-only commit calls
`apply_source_metadata_only`, which is the crate half alone. Snapshot-and-restore
could not hold this line: it silently missed every store a later `switch_playlist`
learned to revoke — which is how a queued listener dedication kept airing while
the promise that owned its song was revoked underneath it — and it could not undo
the `pinned_track_revision` / `force_next_revision` bumps that the guarded clears
perform, leaving every handoff and pending request that recorded a revision
unable to clear the slot it owns.

**Music admission reservations follow the segment, not the crate.** Every queued
*rotation* music segment owns a `music_admission_reservations` entry (norm-cache
rescue, continuity-cache fills, the emergency tone and restart-handoff music are
queued without one, and are unaffected by any of this), and
`Segment.mark_playback_started()` starts it by calling `commit_music_admission`
through the segment's playback-start callback. A `False` there is not a warning:
playback releases the segment and moves on, so revoking a *surviving* segment's
reservation deletes it as surely as dropping it from the queue, only later and
silently. Four *source-replacement* paths keep a segment across a crate change
and therefore keep its reservation (the in-place filters — `_apply_ban` and
dismissing a listener request — also rewrite `state.playlist`, but never touch
the reservation map, so they keep them by construction): `apply_source_metadata_only` (preserves the whole queue);
`_apply_loaded_source` and `purge_pool`, which both call `switch_playlist(...,
preserve_reservation_ids=...)` with `_protected_reservation_ids` — the ids still
protected after the runway pass, covering the on-air dedication's promised song,
the assetless branch's preserved runway head, and `purge_pool`'s
deliberately-kept head; and `restore_playlist_if_still_empty`, which refills an
emptied crate without purging anything. `_protected_reservation_ids` reads
`state.continuity_slot` as well as the queue, because a capacity-constrained
runway parks a survivor out of band — reading only the queue leaves precisely
the assetless last-runway case unfixed. A retained starter reservation keeps its
`starter_cycle_reserved` slot; `_sync_starter_cycle` rebuilds that set from the
live reservations the next time the cycle is *consulted* and finds the catalogue
changed (a crate assignment alone triggers nothing), so a crate that loses and
regains a starter track cannot offer a copy that is still queued.

Text-direction expansion performs its network work before entering
`source_switch_lock` and captures that epoch at the serialized commit boundary;
an unrelated Stop/Resume transaction that has already completed therefore does
not incorrectly turn a current commit into metadata-only work. A later control
that crosses the actual commit boundary still wins. Likewise, a slow admin
play-next download that becomes metadata-only may retain its accepted
`pinned_track`/`force_next=MUSIC` ownership for the producer, but it never queues
or reserves audio from the stale request.

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
- missing or disabled `external-media` leaves the local-or-starter base untouched;
  external-only standalone operations report the capability boundary, and
  add-ons return the locked actionable `403` without importing or executing
  `yt-dlp`
- Jamendo discovery, license, URL, network, normalization, timeout, race,
  cancellation, or restart failures destroy the single-use artifact/lease and
  immediately continue with local/starter music
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
| `mammamiradio/core/setup_status.py` | Canonical guided setup projection, including First Listen source, speaker, verification, and privacy stages |
| `mammamiradio/core/first_listen.py` | Durable policy-free First Listen receipt and feature-era install-origin witnesses |
| `mammamiradio/core/first_listen_show.py` | Packaged client-local mini-show eligibility and chunk iteration |
| `mammamiradio/core/sync.py` | SQLite database initialization and schema migration |
| `mammamiradio/media/starter.py` | Canonical starter-manifest loading, release readiness, attribution, and cycle construction |
| `mammamiradio/playlist/playlist.py` | Local-or-starter base loading plus optional standalone external source compatibility |
| `mammamiradio/playlist/local_library.py` | Recursive local-root discovery, scan status, playlist reconciliation, and background scanner task |
| `mammamiradio/playlist/downloader.py` | Local/starter resolution plus capability-gated standalone external handling |
| `mammamiradio/playlist/jamendo_transient.py` | One-lease/one-artifact Jamendo discovery, streaming normalization, and destruction lifecycle |
| `mammamiradio/hosts/memory_extractor.py` | Post-air banter memory extraction for persona updates and LLM reaction cues |
| `mammamiradio/playlist/song_cues.py` | Machine-derived per-track memory: anthem detection, skip-bit detection, stored reaction cues |
| `mammamiradio/playlist/track_rationale.py` | "Why this track?" rationale generation for listener UI |
| `mammamiradio/playlist/track_rules.py` | Per-track personality rules flagged by admin via `/api/track-rules` |
| `mammamiradio/scheduling/scheduler.py` | pacing rules and upcoming preview |
| `mammamiradio/scheduling/producer.py` | segment generation pipeline |
| `mammamiradio/scheduling/clip.py` | WTF clip extraction from ring buffer, save, cleanup; keepsake eligibility gates, exact-segment extraction, durable keepsake save |
| `mammamiradio/release_campaign.py` | Packaged release-beat manifest loading and bounded on-air campaign state (`cache/release_campaign_ledger.json`) |
| `mammamiradio/restart_handoff.py` | Post-restart music continuity spool: producer writes safe recent segments, startup admits them into the queue (`cache/restart_handoff/`) |
| `mammamiradio/hosts/scriptwriter.py` | Anthropic/OpenAI prompts for banter and ad copy (TODO: split — see cathedral plan PR 6) |
| `mammamiradio/hosts/prompt_world.py` | Prompt-fiction data: expression banks, host fingerprints, exchange-shape/lore banks, style directives, Chaos/Festival mode blocks |
| `mammamiradio/hosts/relationship.py` | Roster-aware exchange-shape/lore selection and in-memory recency rotation |
| `mammamiradio/hosts/transitions.py` | Transition rewrite openers + anti-repeat stem/massage helpers |
| `mammamiradio/hosts/fallbacks.py` | Stock fallback copy: chaos stock lines, ad-break intros/outros |
| `mammamiradio/hosts/persona.py` | Listener persona: compounding memory, arc phases, motif tracking, session counting |
| `mammamiradio/hosts/context_cues.py` | Time-of-day and cultural context for prompts |
| `mammamiradio/hosts/ad_creative.py` | Brand and voice selection, campaign-spine sampling for ad breaks |
| `mammamiradio/audio/imaging.py` | station imaging selector and safe schema-v2 ad-recipe resolver |
| `mammamiradio/audio/synth_cache.py` | reusable `synth_*.mp3` cache for generated ad/imaging layers |
| `mammamiradio/audio/normalizer.py` | ffmpeg helpers for normalization, mixing, tones, bumpers, bleed, and SFX |
| `mammamiradio/audio/audio_quality.py` | Audio quality gate: duration and silence checks before segments reach the queue |
| `mammamiradio/audio/tts.py` | TTS synthesis (Edge, OpenAI, Azure Speech, ElevenLabs) |
| `mammamiradio/audio/voice_catalog.py` | Edge, OpenAI, and curated Azure voice ID catalogs |
| `scripts/audition_tts_voices.py` | Local audition clips and manifest generation for configured/catalog TTS voices |
| `scripts/first-listen-lab.sh` | Isolated local Home Assistant + VLC speaker lab lifecycle and radio-only reset commands |
| `mammamiradio/home/ha_context.py` | Home Assistant polling, heuristic mood classification, optional LLM scene-namer, reactive triggers |
| `mammamiradio/home/ha_playback.py` | Home Assistant WebSocket speaker discovery and fixed Media Source playback dispatch |
| `mammamiradio/home/context_value.py` | Privacy-safe classification of a detached preview as useful, ambient-only, or empty |
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
