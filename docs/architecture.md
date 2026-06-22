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
                |
                +-> /public-status and /status
```

## Startup flow

`mammamiradio.main:startup()` does eight things:

1. Loads `radio.toml` and `.env` through `config.py`.
2. Validates the config and applies legacy migration like `station.bitrate -> audio.bitrate`.
3. Purges suspect cache files (< 10KB, likely failed downloads) and evicts old cache entries.
4. Restores persisted source selection from `cache/playlist_source.json`, then fetches the playlist by walking the priority chain (charts → Jamendo → local `music/` → bundled demo assets → built-in `DEMO_TRACKS`) and falling through to the next source whenever a tier is gated off, unconfigured, or empty.
5. Initializes the clip ring buffer for WTF clip sharing.
6. Restores persisted `chaos_mode_active` from `MAMMAMIRADIO_CHAOS_MODE` or HA add-on `/data/options.json` without arming a first strike.
7. Creates shared app state, then launches:
   - `run_producer()` to fill the lookahead queue
   - `run_playback_loop()` to stream queued audio
8. Logs a one-line boot summary with resolved config dir, audio source, API key presence, HA status, and track count.

### Heading overlay

The admin Rotazione tab can steer the next stretch of music without replacing the
base playlist:

- `POST /api/heading {"seed": "classic://italian/80s"}` loads one of the existing
  classic Italian era sources, filters the operator blocklist, dedupes against the
  live pool, tags newly blended tracks with the active `Heading.id`, and bumps
  `playlist_revision` once. A zero-result import returns warm operator copy and
  does not arm narration.
- `POST /api/heading/clear` is manual Back to auto. It clears `StationState.heading`
  and deletes `cache/heading.json`; already blended tracks remain in rotation and
  age out naturally. There is no purge and no audio interruption.
- `/api/playlist/load` is a true source replacement and clears the active heading
  plus `heading.json`, so a restart cannot reapply an old course over a freshly
  loaded base.

`cache/heading.json` is an overlay, separate from `playlist_source.json`. Reads are
corrupt/missing tolerant and return no heading rather than failing boot. After the
startup base playlist is fetched and blocklisted, startup re-fetches the persisted
heading seed, re-tags matching tracks, and blends any new heading tracks into the
pool. If that boot fetch fails, returns no playable tracks, or adds no new tracks
after dedupe, startup deletes `heading.json` and continues in auto mode.

Narration is selection-driven, not button-driven. `StationState.select_next_track()`
arms `heading_pending_announcement` only when the producer accepts a track tagged
with the active heading id for airing. The next host break consumes that dedicated
slot at prompt-build into a mood-noticing block; it does not reuse or overwrite
`ha_pending_directive`. The host observes that someone asked for, or is in the mood
for, the selected era rather than claiming the station is currently playing it or
returning there. Because the line asserts a request, not present playlist state, it
is intentionally allowed to air even if Back to auto or another heading lands while
the banter is rendering. Consuming the notice marks `heading.announced` and persists
that flag best-effort so restarts do not redundantly re-notice. The music turn
always remains best-effort and never blocks or delays audio.

## Segment production

`scheduler.py` is the single source of truth for pacing:

- the first segment is always music
- ad breaks trigger when `songs_since_ad >= songs_between_ads`
- banter triggers when `songs_since_banter` crosses the configured threshold, with a small random jitter outside preview mode

`producer.py` turns that pacing decision into actual audio files:

- `MUSIC`
  - uses local `music/` files, then `yt-dlp` for chart tracks; when all candidates fail the audio quality gate, recycles the last-known-good music norm file, then drops the track and lets the playback rescue path handle the gap — silent audio is never queued
  - normalizes output before queueing
- `BANTER`
  - asks Claude (or OpenAI as fallback) for structured dialogue JSON
  - synthesizes one line per host via the configured TTS engine (see [TTS architecture](#tts-architecture) below)
  - passes generated host speech through the imaging layer so banter and news can sit over a quiet music bed, falling back to a synthetic pad on cold starts
  - preserves running jokes in `StationState`
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
loudnorm on ffmpeg 8.x / Pi aarch64) closed, and it holds the same `_NORM_SEM` slot as
`normalize()` so the extra encode respects the Pi 2-FFmpeg ceiling.

The pipeline is **best-effort and instant-audio-safe**: a stage failure leaves the
prior audio in place and never raises, and emergency / bridge / rescue fills skip the
pipeline entirely so a dead-air rescue is never delayed by an extra encode (leadership
principle #2, INSTANT AUDIO). The skip is driven by an explicit `rescue` flag stamped
where each bridge/rescue is built (`_is_rescue_fill()`), **not** by sniffing overloaded
metadata keys: a canned clip in normal rotation (shareware gold clips / Demo mode) is
`canned=True` but is **not** a rescue, so it is still coloured — otherwise the first
host break a new user hears would air studio-clean next to FM music, the exact seam
this stage removes. The chaos and reactive-interference content stages slot in
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

### Queue commit (the per-path gate matrix)

Every produced segment reaches the playback queue through a small set of commit
paths, and they DELIBERATELY differ in which gates they run — the differences are
the contract, not an oversight. Most segments commit in the `run_producer`
main-loop epilogue (the `if segment:` block); bridges and the startup prewarm
enqueue directly through `_enqueue_with_egress()`. The matrix below is pinned by
`tests/scheduling/test_queue_commit_contract.py`.

| Commit path | stopped discard | stale gate (playlist / chaos) | blocklist gate | egress (FM) | queue op | up-next shadow row |
|---|---|---|---|---|---|---|
| Main-loop commit (music + all generated speech: banter, news flash, ad, station-id, sweeper, time-check) | yes | **yes — pre-egress, shared epilogue** | yes (music only) | yes | append | **yes** |
| Operator air-next (forced trigger) | yes | **yes — same epilogue; a discard releases `operator_force_pending`** | yes | yes | **front-insert** (may drop the furthest-future tail) | yes (at head) |
| Outer error-recovery rescue (`rescue=True`, built in the loop body) | yes | yes (epilogue) | yes | **skipped (rescue)** | append | **yes** |
| Inner bridge / drain-recovery rescue (direct enqueue) | yes | **no** — instant-audio: a fill must air regardless of source state | yes | **skipped (rescue)** | append | **no — airs invisibly** |
| Prewarm (startup pre-roll) | yes | **yes** — discards a stale-source render mid-warm | yes | yes | append | **no** |

- The **main-loop** stale gate compares `generation_revision` (captured once per loop
  iteration) against `state.playlist_revision` (and `chaos_cutover_epoch` against
  `generation_chaos_epoch`), and it runs **pre-egress only** — those paths do not
  re-check after the awaited egress pass, so a slow/enabled egress colour pass widens
  their window (current behavior, not a guarantee).
- **Prewarm** keys on `source_revision` (bumped only by a true source switch), not the
  broad `playlist_revision`, so a benign in-place edit (shuffle/add/move/enrich) keeps
  the on-source pre-roll. It also passes a **post-egress** stale check to the funnel
  (`_enqueue_with_egress(..., stale_check=...)`), so a source switch landing during the
  egress encode discards the pre-roll at the last moment instead of putting it into the
  queue the switch route just purged.
- Inner bridge / drain-recovery rescue and prewarm air with **no shadow row**, so
  they don't appear in the "Up Next" projection until they reach the head (outer
  error-recovery rescue, built in the loop body, *does* get a row). The streamer
  reconciles the shadow list as it consumes the queue.
- The blocklist gate is the funnel's last-resort drop for a banned song that a
  mid-render ban race slipped past the ingest doorways (music only). It drops the
  audio on every path, and the commit path propagates that drop — a banned song
  dropped mid-commit leaves no up-next shadow row and advances no counters.

### Dynamic LLM routing (which model voices each task)

Script generation never names a model in code. Each call site asks for a model by
**role**, and `resolve_model()` in `mammamiradio/core/config.py` resolves it:

```text
task (caller)  ──routing──▶  role  ──active profile──▶  catalog key  ──catalog──▶  model id
  "banter"                  "creative"     premium/balanced/economy        "opus"      "claude-opus-4-8"
  "transition"              "fast"                                          "haiku"     "claude-haiku-..."
```

- The `[models]` block in `radio.toml` is the only place model IDs live: a
  per-provider `catalog`, a `routing` map (task→role), and named `profiles`
  (the admin "quality dial": `premium` | `balanced` | `economy`).
- `resolve_model()` is **total** — an unrouted task, missing profile, or missing
  catalog key resolves through `default_profile` to a real model ID, never a crash
  (a crash here would be dead air). The only hardcoded constant is the role name
  `DEFAULT_ROLE = "creative"`; no model ID is baked into code except the built-in
  `DEFAULT_MODELS` cold-start safety net.
- A missing or malformed `[models]` block **degrades** to `DEFAULT_MODELS` so the
  station always boots and airs; it never fails boot.
- `fast` (transitions) is pinned to the lowest-latency model in every profile.
- The OpenAI fallback resolves the **same role** on the OpenAI side, so a transition
  falls back to the fast OpenAI model and banter to the creative one.
- The quality profile hot-swaps live via `POST /api/quality` (admin) with no restart
  and no queue purge — only the next generated segment changes model.

Every produced segment becomes a temporary MP3 on disk and is pushed into `asyncio.Queue[Segment]`.
Before queueing, `mammamiradio/audio/imaging.py` may prepend transition stings at music/speech boundaries and mix motif stings under sweepers. Optional operator assets live under `mammamiradio/assets/imaging/`; otherwise FFmpeg-generated stings and beds are used.

Bounded state lists (`played_tracks`, `running_jokes`, `segment_log`, `stream_log`, `ad_history`, `recent_outcomes`) use `deque(maxlen=N)` for automatic memory management — no manual truncation needed.

**Callback Director (cross-domain verbal gags).** A gag planted in DJ banter can resurface once inside an unrelated news flash or ad — a rare, cross-domain "callback". `hosts/verbal_gag_ledger.py` (`VerbalGagLedger`, in-memory, session-ephemeral) holds banter-seeded gags and reuses `home/gag_select.py`'s `weighted_offer` (the same weighted-pick + 0.55 silence roll that `home/evening_memory.py`'s `EveningLedger` uses for HA-event gags). Lifecycle, all at QUEUE time so a discarded segment never plants or burns a gag: banter's `new_joke {text, punch}` is stashed on `state.pending_verbal_gag` and committed to the ledger in the banter success callback; before a flash/ad the producer calls `offer(contrasting_to=...)` and passes at most one gag to the scriptwriter (which injects a "land this here" instruction, or omits the key entirely); the gag is hard-retired after one travel, and only when the generator reports it actually landed (`callback_used`). Flash/ad prompts no longer carry the full `running_jokes` list — `running_jokes` stays banter's self-reference + persona-store store.

**Evening running gags (HA-event callbacks).** `home/evening_memory.py`'s `EveningLedger` tallies repeated discrete home toggles across an evening and surfaces a deferred, approximate callback ("the coffee machine, on again tonight") into banter via the STASERA prompt block. Gag-candidacy is decided by device **domain** (not hardcoded entity_ids), so it works on any operator's home out of the box: `switch`/`fan`/`lock`/`vacuum`/`binary_sensor` toggles are gag-worthy, while `sensor`/`climate`/`media_player`/`weather`/`light` and `person.*` are not. Operators tune this via `[home.running_gags]` in `radio.toml` (`domain_allowlist` replaces the default domain set; `entity_allowlist` restricts to specific entity_ids; `entity_denylist` silences chatty entities) — parsed into `core/config.EveningGagsSection`, degrade-to-default on malformed input. An evening "session" ends after `EVENING_GAP_SECONDS` (3.5h) with no real home activity — `last_active` advances only on real activity (excluding numeric drift, `person.*`, device-availability flaps, and passive `weather`/`sun` changes), so neither radio-cadence polling nor passive environmental events can keep a quiet evening alive forever — or at the 4am day rollover.

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

The system uses two independent boolean flags in a frozen `Capabilities` dataclass (`mammamiradio/core/models.py`, with detection and serialization in `mammamiradio/core/capabilities.py`):

| Flag | Source | What it enables |
| --- | --- | --- |
| `anthropic` | `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` present | Live AI-generated banter and ads |
| `ha` | `HA_TOKEN` + integration enabled | Ambient home context in banter |

The dashboard derives a tier label from these flags: Demo Radio, Full AI Radio, Connected Home. `GET /api/capabilities` returns flags, tier, and a guided `next_step` hint (what the user should do next).

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

Once playback is running, the producer's recovery layers (last-known-good music recycle, demo-asset rescue, forced banter) keep the queue from starvation if a source disappears mid-session. Silent audio is never queued intentionally.

### Operator song blocklist

Because every source above is re-fetched fresh on startup, an in-memory "remove" would reappear after a restart. The operator blocklist makes a ban durable. It persists to `cache_dir/blocklist.json` as `{serialized_key: {display, banned_by, banned_at}}`, keyed by the single canonical identity `normalized_track_key(track) = (artist.strip().lower(), title.strip().lower())` (the same key used for playlist dedup, so a ban holds across sources even when the per-source track id differs). The store is best-effort and corrupt-tolerant: a missing or malformed file loads as empty and never raises into the audio path; writes are atomic (`tmp` + `os.replace`).

Enforcement is a single primitive, `playlist.filter_blocklisted(tracks, blocklist)`, applied at every doorway where tracks enter `state.playlist`: startup (`main.py`), source switch (`_apply_loaded_source`), the mid-session chart refresh (`fetch_chart_refresh`), and the external/listener download commit (`_commit_external_download`). The norm-cache **rescue** path is a separate doorway — it serves cached audio directly without passing through `state.playlist`, so `select_norm_cache_rescue` (in `audio/norm_cache.py`) drops blocklisted cache files itself, matching each file's `{title, artist}` sidecar against `state.blocklist`; a banned song never re-airs even when the queue starves and recovery kicks in (if every cache file is banned the rescue degrades to the next layer — canned clip / forced banter — never to a banned song). The external/listener commit returns a distinct `"banned"` status (not `"dropped"`): the admin gets an honest "it's banned" notice and a listener request fails loudly (`song_error`) instead of spinning on "searching…". Bulk `/api/playlist/enrich` honors the blocklist; only an explicit single `/api/playlist/add` bypasses it as an intentional override. Banning (`POST /api/track/ban`, or the per-row `/api/playlist/remove`) also clears a matching `pinned_track` and synchronously drops any not-yet-started queued segment of the song — the currently-airing segment finishes untouched, so a ban never causes dead air. A bulk ban that would leave fewer than `MIN_ROTATION_AFTER_BAN` songs (or that would empty an already-small pool) is refused with a warm message rather than starving the pool onto the rescue path; a single per-row removal stays exempt. The persist call is best-effort — when `blocklist.json` can't be written the ban still holds for the session and the API echoes `persisted: false` so the admin UI says "banned for now, may come back after a restart" instead of promising permanence. `POST /api/track/unban` and `GET /api/track/banlist` back the admin "Banned" manager. Listener thumbs-down voting is a separate later slice; this layer is operator-only.

## TTS architecture

Each host declares a TTS engine in `radio.toml`: `engine = "edge"` (default), `engine = "openai"`, `engine = "azure"`, or `engine = "elevenlabs"`. Dedicated ad voices and the sonic-brand sweeper voice use the same provider-routing fields.

**Edge TTS** (Microsoft): free, no API key. Each host maps to an Azure Neural voice (e.g., `it-IT-GiuseppeNeural`). SSML prosody tags (rate, pitch) are derived from the host's personality axes for voice differentiation.

**OpenAI TTS** (`gpt-4o-mini-tts`): requires `OPENAI_API_KEY`. Each host maps to an OpenAI voice (e.g., `onyx`). Personality-aware delivery instructions are generated from the host's energy, warmth, and chaos axes — the model interprets these as acting direction, not just static parameters.

**Azure Speech TTS**: requires `AZURE_SPEECH_KEY` and `AZURE_SPEECH_REGION`. Useful for official Italian voices and HD voices while keeping the existing Edge voice family as fallback.

**ElevenLabs TTS**: requires `ELEVENLABS_API_KEY` and operator-provided voice IDs. Intended for custom character voices in ads, sweepers, and guest bits.

Fallback chain: cloud TTS failure or missing credentials → `edge_fallback_voice` (so the role falls back to its own Edge voice, not a stranger) → Edge runtime fallback/silence recovery.

A singleton OpenAI client is reused across OpenAI TTS calls for connection pool efficiency.

## Compounding listener memory

`persona.py` maintains a persistent listener profile in SQLite (`cache/mammamiradio.db`). The persona tracks:

- **Session count**: how many times the listener has tuned in (10-minute gap = new session)
- **Arc phase**: relationship stage computed from session count — stranger, acquaintance, friend, or old_friend. Each phase shapes callback budgets and joke styles. Milestone sessions (1, 5, 10, 25, 50, 100) inject subtle acknowledgment directives into prompts.
- **Motifs**: the last 20 played tracks, so hosts can reference past music naturally
- **Theories**: LLM-generated guesses about who the listener is
- **Running jokes**: cross-session callbacks that build familiarity
- **Callbacks used**: structured format `{"song": "...", "context": "..."}` recording which songs were referenced and why

During banter generation, the persona is loaded into the prompt via `<listener_memory>`. Claude's response includes `persona_updates` (new theories, jokes, callbacks) which are persisted back to SQLite. First-time listeners get curiosity and intrigue. Returning listeners get inside jokes, personal references, and phase-aware banter depth.

Instruction-like patterns in persona entries are filtered before storage (matching the `ha_context` sanitizer) to prevent stored prompt injection across sessions.

## Song cues

`song_cues.py` builds machine-derived per-track memory in SQLite (`cache/mammamiradio.db`), separate from the persona:

- **Anthem detection**: a track played 3+ times and never skipped becomes an anthem. The cue is stored with confidence "anthem".
- **Skip-bit detection**: a track skipped 2+ times gets a skip-bit cue. When the listener skips a known skip-bit track, the hosts can react ("caught you again").
- **LLM reaction cues**: during banter generation Claude can generate a free-text reaction cue for the current track (e.g., "sempre questa canzone sul tramonto"). These are stored and reinjected into future banter prompts for that track.

Cues appear in banter prompts as a `TRACK MEMORY` block alongside operator-flagged rules from `track_rules.py`. The `youtube_id` from live playback state is used to key cues rather than trusting LLM echoes, preventing orphan rows from hallucinated IDs.

Cue text is sanitized via `_sanitize_prompt_data` on the read path before injection, closing a cross-session prompt injection vector.

## Optional Home Assistant context

If `[homeassistant].enabled = true` and `HA_TOKEN` is present:

- `ha_context.py` polls the Home Assistant REST API state snapshot and filters it through a default-deny privacy layer
- sensitive domains (`device_tracker`, `camera`, `alarm_control_panel`), free-text helper domains (`input_text`, `text`), and telemetry/config entities are excluded before prompt assembly
- `person.*` is kept as home/away presence only (GPS, `user_id`, and tracker attributes stripped) so arrival greetings and the empty-home mood still work; person events never reach `/public-status`
- allowed entities are scored by domain salience, recent changes, area metadata, event activity, and curated-label overrides
- the prompt receives a bounded top slice (12 entities by default, capped at 2000 characters) rather than the full home snapshot
- hand-tuned entity labels (curated tier) remain authoritative; unknown entities resolve through a generated catalog backed by Anthropic (`home/catalog.py`, cached locally), then a sanitized HA display name plus area metadata, and are dropped entirely rather than letting a raw entity ID reach a host prompt
- event diffing, mood classification, and weather narrative arcs continue to feed the existing scriptwriter fields
- 7 reactive triggers fire on specific state changes (coffee machine, door unlock, vacuums, arrivals, terrace lights)
- banter references are tiered: 1 item by default, up to 2 when a mood scene is active (mood counts toward cap)
- weather-mood fusion allows hosts to connect outdoor conditions to indoor activity
- the weather news flash grounds itself in the real Home Assistant forecast when available, then spins it into absurd local color; with no forecast (HA disconnected or unsupported) it falls back to the fully fictional meteo prompt, so the segment never goes silent. `NEWS_FLASH` shares the same HA-context refresh gate as banter/ad, so the flash reads a freshly refreshed forecast (bounded by the weather cache TTL plus one poll interval) rather than the startup snapshot. The arc follows the station language: Italian stations use `state.ha_weather_arc`, every other language uses `state.ha_weather_arc_en` — never the Italian arc — and the stock fallback line is localized too
- numeric state passthrough in `ha_enrichment.diff_states()` ensures power sensors generate events
- the listener dashboard shows a "Casa" card with mood, weather, and recent events via `ha_moments` in `/public-status`
- the admin panel shows full HA details (mood, weather arc, events summary, pending directives, scored entities, and privacy filter counts) via `ha_details` in `/status`
- scored entities and privacy filter counts are admin-only and never appear in `/public-status`
- `push_state_to_ha` always sets `entity_picture` on `media_player.mammamiradio` to an absolute http(s) image: the track's cover (`Track.album_art`) while a song plays, and the station logo for host talk, ads, music with no cover, and idle/stopped. The logo fallback is required because HA's media-control card does not clear a removed `entity_picture` — it keeps the last cover — so omitting it would leave the previous track's art on screen during a news flash. The logo URL is `[brand] artwork_url` (absolute http(s) only; relative paths are rejected because HA resolves `entity_picture` against its own origin), defaulting to the bundled station logo. `media_image_url`/`media_image_remotely_accessible` are intentionally omitted (inert for a state pushed via the REST API rather than a media_player integration component)

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
ha_context.py: lightweight 5s poll detects idle transition (separate from 60s full fetch).
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

| Route | Method | Access | Description |
| --- | --- | --- | --- |
| `/` | GET | Public | Listener page. Over trusted HA ingress the admin panel is served instead. |
| `/listen` | GET | Public | Alias of `/` for backwards compatibility |
| `/admin` | GET | Admin | Admin control room panel |
| `/dashboard` | GET | Admin | 301 redirect to `/admin` (legacy) |
| `/sw.js` | GET | Public | PWA service worker |
| `/static/{filename:path}` | GET | Public | PWA static assets (manifest, icons) |
| `/stream` | GET | Public | Infinite MP3 stream |
| `/healthz` | GET | Public | Liveness probe with process uptime |
| `/readyz` | GET | Public | Readiness probe with queue depth and startup status |
| `/public-status` | GET | Public | Current segment, recent log, the real queued segments (`upcoming_mode` is `queued` or `building`), and `stream.audio_format` (the canonical encoding contract — see "Stream audio format metadata" below) |
| `/status` | GET | Admin | Full admin JSON: queue depth, uptime, scripts, HA context, errors, `provider_health`, `runtime_status` (normalized provider state, session failover event history, and `bridge_health` rescue-bridge telemetry — see operations.md "Reading queue-rescue health"), `production` (the live "In produzione" feed — `current` is the phase the producer is building right now, `recent` is a bounded trail of just-finished work; admin-only, never in `/public-status`), and `playlist_page` (`{total, offset, limit, has_more, revision}`). Accepts `?playlist_offset=0&playlist_limit=80` (max 200) for lazy loading. |
| `/api/setup/status` | GET | Admin | First-run setup status, detected run mode, and station mode |
| `/api/setup/recheck` | POST | Admin | Re-run setup probes |
| `/api/setup/provider-check` | POST | Admin | Active, secret-safe Anthropic/OpenAI/Azure Speech/ElevenLabs connectivity check |
| `/api/setup/addon-snippet` | GET | Admin | Copy-friendly Home Assistant add-on config snippet |
| `/api/shuffle` | POST | Admin | Shuffle playlist |
| `/api/skip` | POST | Admin | Skip current segment |
| `/api/purge` | POST | Admin | Remove queued segments |
| `/api/queue/remove` | POST | Admin | Remove one queued segment by stable `id` (or legacy `index`) |
| `/api/playlist/remove` | POST | Admin | Remove track by index |
| `/api/playlist/move` | POST | Admin | Move track with `{from, to}` |
| `/api/playlist/move_to_next` | POST | Admin | Move track to position 0 in upcoming |
| `/api/playlist/add` | POST | Admin | Add a track to the playlist |
| `/api/playlist/load` | POST | Admin | Load a playlist by URL |
| `/api/hosts` | GET | Admin | List hosts with personality settings |
| `/api/hosts/{host_name}/personality` | PATCH | Admin | Patch host personality axes (energy, warmth, chaos) |
| `/api/hosts/{host_name}/personality/reset` | POST | Admin | Reset host personality to defaults |
| `/api/pacing` | GET | Admin | Current pacing configuration |
| `/api/pacing` | PATCH | Admin | Patch pacing fields (songs between banter, ad spots per break, etc.); malformed payloads return 400, values are clamped to safe floors/ceilings |
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
| `/api/playlist` | GET | Admin | Paginated playlist window; `?offset=0&limit=80` (max 200); returns `{tracks, total, offset, limit, has_more, revision}` |
| `/api/search` | GET | Admin | Search playlist and external sources; pagination via `offset`/`limit` (max 50 local, max 10 external) and `external_offset`/`external_limit`; `include_external=false` skips yt-dlp when the client has exhausted web results; returns `{results, external, total, has_more, external_has_more, …}` |
| `/api/playlist/add-external` | POST | Admin | Add external track from search results; accepts optional `album_art` URL (http/https only, validated server-side) |
| `/api/interrupt` | POST | Admin | Immediately interrupt the stream — hosts deliver pissed/urgent banter with a custom directive. Body: `{"directive": str, "urgency": "pissed"\|"urgent"\|"gentle"}`. 60s cooldown enforced; returns 429 on spam. |
| `/api/hot-reload` | POST | Admin | Reload `prompt_world.py`, `transitions.py`, `fallbacks.py` then `scriptwriter.py` (leaves-first) in-place via `importlib.reload()` — stream continues uninterrupted, next banter uses new code. Requires `--workers 1`. |

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

`source_switch_lock` (asyncio.Lock on `app.state`) serializes `/api/playlist/load` so only one source change runs at a time. The endpoint triggers immediate cutover: the segment queue is purged, the current segment is skipped, and playback begins from the new source. The producer uses a `playlist_revision` counter on `StationState` to detect and discard segments generated for a stale source. `/api/shuffle` also increments `playlist_revision` so any in-flight producer work targeting the old order is discarded and rebuilt against the new sequence.

## Failure model

This repo is biased toward "keep the station on air."

- producer exceptions insert a short silence segment instead of crashing the app
- script generation failures fall back to OpenAI when configured, then to stock copy
- chaos first-strike script failures use subtype-specific stock lines and report `provider_health.chaos.last_degraded_reason = "script_fallback"`; chaos audio failures are counted separately as `audio_failure`
- missing yt-dlp falls back to local files or demo tracks
- missing Home Assistant context is ignored
- missing ad brands disables ads rather than killing startup

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
| `mammamiradio/playlist/downloader.py` | local-file, yt-dlp, and placeholder music fallback |
| `mammamiradio/playlist/song_cues.py` | Machine-derived per-track memory: anthem detection, skip-bit detection, LLM reaction cues |
| `mammamiradio/playlist/track_rationale.py` | "Why this track?" rationale generation for listener UI |
| `mammamiradio/playlist/track_rules.py` | Per-track personality rules flagged by admin via `/api/track-rules` |
| `mammamiradio/scheduling/scheduler.py` | pacing rules and upcoming preview |
| `mammamiradio/scheduling/producer.py` | segment generation pipeline |
| `mammamiradio/scheduling/clip.py` | WTF clip extraction from ring buffer, save, cleanup |
| `mammamiradio/hosts/scriptwriter.py` | Anthropic/OpenAI prompts for banter and ad copy (TODO: split — see cathedral plan PR 6) |
| `mammamiradio/hosts/prompt_world.py` | Prompt-fiction data: expression banks, host fingerprints, style directives, Chaos/Festival mode blocks |
| `mammamiradio/hosts/transitions.py` | Transition rewrite openers + anti-repeat stem/massage helpers |
| `mammamiradio/hosts/fallbacks.py` | Stock fallback copy: chaos stock lines, ad-break intros/outros |
| `mammamiradio/hosts/persona.py` | Listener persona: compounding memory, arc phases, motif tracking, session counting |
| `mammamiradio/hosts/context_cues.py` | Time-of-day and cultural context for prompts |
| `mammamiradio/hosts/ad_creative.py` | Brand and voice selection, campaign-spine sampling for ad breaks |
| `mammamiradio/audio/imaging.py` | station imaging selector for transition stings, sweeper stings, and talk beds |
| `mammamiradio/audio/normalizer.py` | ffmpeg helpers for normalization, mixing, tones, bumpers, bleed, and SFX |
| `mammamiradio/audio/audio_quality.py` | Audio quality gate: duration and silence checks before segments reach the queue |
| `mammamiradio/audio/tts.py` | TTS synthesis (Edge, OpenAI, Azure Speech, ElevenLabs) |
| `mammamiradio/audio/voice_catalog.py` | Edge, OpenAI, and curated Azure voice ID catalogs |
| `scripts/audition_tts_voices.py` | Local audition clips and manifest generation for configured/catalog TTS voices |
| `mammamiradio/home/ha_context.py` | Home Assistant polling, mood classification, reactive triggers |
| `mammamiradio/home/catalog.py` | Generated device-label catalog: curated overrides, Anthropic-backed generation, four-tier resolver |
| `mammamiradio/home/ha_enrichment.py` | Pure HA event derivation: state diffing, event pruning, numeric passthrough |
| `mammamiradio/web/streamer.py` | HTTP routes, playback loop, clip endpoints, listener fanout (TODO: split — see cathedral plan PR 5) |
| `mammamiradio/web/auth.py` | Request-layer admin auth: `require_admin_access`, CSRF enforcement, trusted-network classification |
| `mammamiradio/web/listener_requests.py` | Listener-request endpoints (submit, public feed, admin queue, dismiss) and the song-wish download background task |
| `mammamiradio/web/og_card.py` | Open Graph share-card PNG renderer |
| `mammamiradio/web/templates/` | `admin.html`, `listener.html`, `live.html` |
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
