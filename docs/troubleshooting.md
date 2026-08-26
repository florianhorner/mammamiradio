# Troubleshooting

Start with the way you run Mamma Mi Radio. Home Assistant app operators and local/Docker operators have different safe first steps.

## Home Assistant app

1. Go to **Settings → Apps → Mamma Mi Radio → Log** and keep the lines around the first warning or error. A healthy start reaches `Producer started`.
2. Open the Web UI and start listening. If you expose port 8000, check `/healthz` and `/readyz`; after a listener accepts audio, a ready station returns HTTP `200` with `"ready": true`.
3. Follow the matching symptom below. If the problem needs a code change or add-on recovery, use the [supported add-on workflow](../ha-addon/mammamiradio/DOCS.md#failure-modes-and-recovery). Do not use the Python virtual-environment commands in the next section against a running Home Assistant app.

## Local source or Docker

For a source checkout, use the project environment and install both the app and developer tools:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e . -r requirements-dev.txt
./start.sh
```

For Docker, keep the first evidence simple:

```bash
docker compose ps
docker compose logs --tail=200
```

If a source run or test reports a missing module such as `dotenv`, activate `.venv` and repeat the install command above. If Docker is unhealthy, keep the first error from `docker compose logs` and use the same symptom guide below.

## Shared readiness checks

If the dashboard is in the first-run setup flow, trust the banner. The station classifies itself as `Demo Radio`, `Full AI Radio`, or `Connected Home` based on AI host keys and whether a prompt-safe Home Assistant context slice is available.

Useful probe endpoints:

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
```

`/healthz` answers "is the process and listener-facing runtime healthy?"; it normally stays HTTP `200` during an intentional Stop but returns `503` for prolonged silence with active listeners. `/readyz` answers "has this running session actually delivered audio to a listener?". Every fresh or Resumed session returns HTTP `503` with `status: "starting"` until at least one listener queue accepts audio; `Producer started`, queued work, and elapsed startup time are not sufficient. Listener acceptance changes the probe to HTTP `200` with `status: "ready"`. A confirmed Force Start remains `starting` while it rebuilds that proof, and prolonged listener silence can return an active session to `starting`. An intentional operator pause is distinct: `/readyz` returns HTTP `503` with `status: "stopped"` until explicit Resume.

## The page loads but the stream itself never plays

If the listener page, dashboard, and `/healthz` all work but the stream fails, check the public station name. `/stream` sends it in the `icy-name` response header, which is limited to latin-1. Common sources of non-latin-1 text include:

- **Smart quotes.** Typing a name on a Mac or iPhone silently substitutes a typographic apostrophe (U+2019) for the straight one.
- **Decomposed accents.** macOS often stores `à` as a plain `a` followed by a separate combining accent (U+0300). The combining mark is not latin-1 either, so a perfectly ordinary `Radio Città` could fail while the same name typed elsewhere worked.

Older builds passed unencodable header text through unchanged. Response construction then failed before audio was sent, returning `500` to listeners while the rest of the app stayed healthy. The add-on log shows Python's escaped form:

```text
UnicodeEncodeError: 'latin-1' codec can't encode character '\u2019' in position 3: ordinal not in range(256)
```

Emoji and CJK characters in the station name or theme caused the same failure. Those builds sent `[station] theme` as `icy-genre`; current builds send the public `[brand] tagline` there instead, so the theme can no longer take the stream down.

A third trigger has nothing to do with punctuation: **any letter outside latin-1**. `Radio Łódź`, `Radio Čačak`, `Rádió Ő` and `Radyo İstanbul` all returned `500` on older builds for the same reason.

Current builds compose the value to NFC and then fold it at the header boundary:

- Accents survive, so `Radio Città` stays `Radio Città`.
- Curly quotes, dashes and ellipses become plain ASCII.
- Letters outside latin-1 degrade to their base letter rather than disappearing. Most reduce by decomposition (`Škoda` becomes `Skoda`). Letters built from a stroke, bar or hook decompose to nothing, so the base letter is read out of the Unicode character name instead: `Radio Łódź` becomes `Radio Lódz` and `Radyo Kırmızı` becomes `Radyo Kirmizi`. Without that step the latin-1 pass deletes the letter outright and the name reads as a typo (`Radio ódz`, `Radyo Krmz`). This is deliberately not a hand-written list of letters. 314 Latin letters fall outside latin-1, and enumerating them is exactly how the first ones got missed. Only the few whose Unicode name contains no base letter at all (`Ŋ ŋ Ə ə Œ œ ẞ ĸ`) are mapped by hand.
- Emoji and CJK, which have no Latin equivalent at all, are dropped.
- Control characters are removed. CR and LF are the header-injection vector; the rest of the C0 range and DEL are illegal field content that a strict server rejects outright. A public `[brand] tagline` needs the same treatment because it bypasses the station-name sanitizer.
- The result is stripped at both ends. Folding an emoji off the edge of a name leaves its space behind, and a header value with leading or trailing whitespace is illegal. Some HTTP implementations (h11) refuse the entire response for it, which would reproduce the original outage by a different route.

If folding leaves no station name, `/stream` uses the default rather than an empty `icy-name`. It omits `icy-genre` when no usable `[brand] tagline` is configured. A tagline is folded before the 64-character limit is applied. Non-string taglines are coerced, so `tagline = 42` in `radio.toml` cannot take the stream down. `[station] theme` remains a scriptwriter prompt and never appears in stream headers.

Nothing about this reaches the listener UI: `/public-status` and the page still carry the full original name, emoji and all. Only the header is folded. If you are on an older add-on and see that error in the log, retype the apostrophe as a straight `'` in the add-on configuration as an immediate workaround; the stream recovers on the next listener connect with no restart.

## The app starts but there is no real music

The attributed twelve-track starter collection is the boot source and needs no
network or provider account. If only continuity or recovery audio airs:

- Open **Music credits**. A valid starter package lists all twelve bundled
  entries even when Jamendo is off.
- Check the app log for a starter manifest, hash, package, or FFprobe error.
- Run `make media-check` in a source checkout. Do not copy around the manifest
  gate or fabricate a receipt; replace the app with a package that passed the
  media proof.
- Confirm `ffmpeg` is available. Runtime excludes a corrupt starter asset and
  advances, while release proof fails the package.

Jamendo cannot repair a broken starter package: it is optional, default-off,
asynchronous enrichment. A Jamendo failure must leave starter/local playback
unchanged. See [Music sources and rights boundaries](music-sources.md).

For the supplied Docker image or Home Assistant app, local MP3s belong in the
  deployment's persistent `/data/music` directory. Populate that data area
  through the deployment's supported storage tooling; do not patch files into
  a running Home Assistant app container. A source checkout instead reads
  repo-local `music/`, or the path set by `MAMMAMIRADIO_MUSIC_DIR`.

**"Clear pool" does not delete local music files, and the songs come back.**
This is by design and is not a bug in the button. `POST /api/playlist/purge`
empties the in-memory rotation only. On the next producer pass with an empty
crate, `_recover_local_rotation` in `scheduling/producer.py` re-scans the music
directory and loads whatever MP3s it finds, so operator-supplied songs return
within one cycle. Local music also outranks the bundled starter catalog at
startup (`playlist.py`), so a stale `/data/music` can shadow the starter set
entirely and make the station look like it is ignoring the bundled crate.
To stop specific songs permanently, use the per-row **✕ Ban** button, which
writes a durable blocklist honored at every ingest doorway including norm-cache
rescue. To remove the files themselves, delete them from the music directory
through the deployment's storage tooling and restart, or switch to an explicit
source. Confirm the starter catalog is ready in Motore first: emptying the music
directory while no other source is available leaves the crate to the recovery
ladder until the next restart, because no runtime path refills rotation from the
starter catalog once the station is already running.

When listeners are connected, `/readyz` flips back to `503 starting` if playback
has been truly silent for more than 30 seconds — silent means no listener queue
accepted audio, not merely that a file was selected. A station bridging an empty
queue on `continuity_1.mp3` is audibly on air and does not count as silent, so the
add-on watchdog is not handed a reason to restart it mid-recovery. The playback
loop first tries one canned clip for the gap, then a recent-aware random
`cache/norm_*.mp3` pick that prefers a song the listener has not just heard,
then eligible bundled starter assets. If there is no cached or starter music,
the packaged clip may repeat; the neutral two-second `emergency_tone.mp3`
remains the final packaged rung. After 60 seconds without any bridge asset the
station requests forced banter so the queue can recover without a restart. If
the station has been explicitly stopped, `/readyz` returns `503 stopped`
regardless of queue depth. Connecting or reconnecting to `/stream` does not
clear the persisted stop; press **Resume** explicitly.

## The same short host line loops every few seconds after Resume or a queue drain

This means the station is living on continuity audio while the producer is still rendering the next segment. Current builds reach for cached music first: on a warm cache, Resume, idle wake-up, and an active-playback drain queue a normalized cached song with no clip in front of it, so the healthy path in the logs is a queued `norm-cache bridge` on its own. On a cold cache, an active drain backed by the packaged starter catalog queues a `verified starter-catalog runway` directly; starter songs do not need normalization-cache copies. The packaged clip appears only when no eligible runway is admitted. The active-drain miss then reads `no music runway queued behind the canned clip`; Resume and idle retain the narrower `no cache music queued behind the canned clip` message. Either way you should not see the same `continuity_1.mp3` line every few seconds.

If the clip still repeats after an active drain, look for a starter
manifest/admission failure first.
Eligible standalone/local normalized cache may still help the rescue picker,
but Jamendo artifacts are deliberately excluded and cannot survive for rescue.

Source-checkout developers with MP3s in the repo-local `music/` directory can
use them with external extraction off and Jamendo off. Those files remain the
operator's responsibility.

## Jamendo stays off or temporarily unavailable

Open **Motore -> Setup -> Music sources** and use the persistent Jamendo row:

- **Finish Jamendo setup** means the current non-commercial acknowledgement is
  missing. No client ID is needed; the station brings its own Jamendo access. A
  migrated operator ID remains disabled until reviewed.
- **Preparing one Jamendo track** is normal. Starter/local music continues and
  a Jamendo miss never delays the next music slot. When the attempt in progress
  is rejecting candidates, the row states the reason for that attempt in plain
  language instead of a running total, so a provider that keeps failing no
  longer reads as healthy.
- **Jamendo temporarily unavailable** is a transient provider/network/audio
  failure, and the row names the dominant reason for the last attempt. Use
  **Check again** once; concurrent retries are coalesced.
- **Jamendo track could not be used** is a provider-wide configuration or
  contract rejection, and the row names the reason. This is where a rejected
  client ID and an unusable working folder appear. Check the saved
  configuration or turn Jamendo off.

Every reason line either says the station is retrying, states explicitly that no
action is needed, or names a step to take. Two blocking failures have an operator
action: access Jamendo will not accept, and a working folder the station cannot
use. Both appear on the **Jamendo track could not be used** row, which never
claims a retry is coming because a blocked provider schedules none. Being asked
to slow down is transient and retries automatically; the reply does not identify
which request ceiling was reached, so no credential change is presented as a remedy.

`rejected_this_attempt`, `dominant_failure_code_this_attempt` and
`attempt_rejections` on the admin `/status` payload describe the most recently
completed discovery pass; they are cleared when a pass succeeds, when settings
change, and when Check again is pressed, so a prepared track never airs under a
failure reason and a replaced run stops being explained. They still hold the
previous pass's values while a new one is running. The dominant code names the
error that ended the pass, which may be a timeout or a provider failure that
appears in no candidate breakdown, so it can be set while the rejection count is
zero. The lifetime `rejected_count` remains for
support and is deliberately never rendered as a bare number. Each pass logs its
breakdown once, carrying codes and counts only — never client IDs, private audio
URLs, or provider response text.

Keep the running app and live cache intact; there is no downloaded Jamendo file
to recover. The integration writes at most one single-use artifact under its boot
temporary directory and deletes it after play or cancellation. No Jamendo
audio or lease record belongs in cache, SQLite, restart handoff, or rescue.

The admin row intentionally says provider confirmation is pending. `ready`
means one technical playback artifact is prepared; it does not mean cleared or
licensed for every station model. For error codes and retain/replace/clear
semantics, see [Music sources and rights boundaries](music-sources.md#configuration-api).

## Air Next or Next track says the station is paused

Air Next only queues an operator pick, and Next track only cuts the current programme, while the station is running. Press **Start** or **Resume** first, then use the control again; a paused station does not keep a hidden pick or skip waiting for later.

## Stop or Resume returns 503

Stop writes `cache_dir/session_stopped.flag` before touching live playback. If
that write fails, the response says nothing changed; fix cache-directory
permissions or free disk space and try Stop again. Do not assume the station
paused merely because the button was pressed.

Resume first reserves readable immediate audio, preferring a warm norm-cache
song, then `continuity_1.mp3`, then `emergency_tone.mp3`. It stays paused if no
runway is readable or if the persisted marker cannot be removed. When every
recovery asset is missing, the response offers **Force Start**. Confirming it is
an explicit corrupt-install escape: it removes the stop marker, requests host
banter, and reports `recovering` while `/readyz` remains `503 starting` until a
listener accepts the rebuilt audio. It is never automatic. Check:

```bash
ls -l cache/session_stopped.flag
ls -lh mammamiradio/assets/demo/recovery/continuity_1.mp3 \
  mammamiradio/assets/demo/recovery/emergency_tone.mp3
bash scripts/check-release-invariants.sh
```

For the add-on, inspect the equivalent paths read-only in the installed image;
do not patch or restart the live container as a test. A healthy Resume log names
`runway_source` and the current `continuity_epoch`. Stop advances that epoch
before it purges, so a later `stale_continuity` discard is expected proof that
pre-Stop work was fenced, not a new audio failure.

Setup can remain **Ready** while playback is paused. That is intentional:
`/api/setup/status` reports configuration/source readiness, while `/readyz` and
authenticated runtime status report transport state. Setup recheck, key repair,
and Home context preview remain available during the pause.

If status appears to name a segment but the control room does not say **On Air**,
look for the two log boundaries. `Selected readable ...` means the file opened
and yielded bytes; it is not listener proof. `Listener-audible segment committed
... accepted_listeners=N` means at least one listener queue accepted the first
chunk. Provider, rescue-rotation, and continuity-air receipts update only at the
second boundary.

## A standalone external-media result sounded like a podcast or audiobook

This section applies only to a standalone installation that deliberately
installed and enabled the `external-media` extra. Both Home Assistant add-ons
omit yt-dlp, so they cannot take this path. Chart metadata can occasionally
surface non-music entries; the source filter rejects obvious podcasts, news
briefings, and audiobooks before they enter the candidate pool.

Expected log signature on chart load:

```
INFO Rejecting non-music chart entry: BBC Studios - Do You Speak English? - Big Train
INFO Chart ingest: filtered 3 non-music entries
```

If a legitimate song is being rejected, check `mammamiradio/playlist/playlist.py::_NON_MUSIC_MARKERS`. The list is deliberately narrow (podcast, bbc comedy, audiobook, news briefing, asmr, …) so real titles almost never trip it. If a real Italian song title legitimately contains one of these markers, remove the marker from the list rather than loosening the check.

## The station keeps rejecting the same track

If a track fails `validate_download` (too short, corrupt, missing duration), the cached copy at `cache_dir/{cache_key}.mp3` used to stay put. The next selection of the same track returned it as a cache hit and the gate rejected it again. Endless loop.

The station purges the file and avoids that cache key for the remainder of the session. The producer's main-loop, prefetch, and prewarm paths skip it before trying another download.

The music quality gate (mostly silence, short normalization output) has a different escape valve — the 3-consecutive-rejection circuit breaker — and does NOT denylist source tracks. A quality-gate rejection drops the cached normalization so it's recomputed next time, but the source track can still be re-picked (the gate failure is usually a normalization artifact, not source corruption).

Expected log signature:

```
WARNING Skipping music track due to invalid download (Some Artist - Some Title): duration too short (8.2s)
WARNING Purged rejected cache artifact abc123.mp3: duration too short (8.2s)
DEBUG No eligible music tracks remain after excluding session-rejected cache keys
```

The denylist is process-local — it clears on restart so a track that was transiently bad gets another chance after the next boot.

## A song a listener requested played twice

A listener song request was pinned to the "play next" slot from two places: once by the background download (`_commit_external_download`) when the file finished, and again by the dedication banter (`_plan_listener_request_block`) the next time a host break was produced — because the request lingers in `state.pending_requests` until that banter's deferred commit applies. Each pin is consumed by `select_next_track` *before* the repeat-cooldown filter runs, so the song aired a second time a few minutes later (the 2026-06-19 "double Linkin Park").

The current ownership chain marks the initial claim with `song_pinned`, reserves every pending matched recording at producer admission and playback, then transfers the exact promised source into a one-shot `ListenerRequestHandoff` after the dedication queues. Queue admission marks that segment and releases the handoff, so later equivalent requests still cannot steal it or make it play anonymously. If you see a repeat, trace the complete reservation → dedication commit → handoff admission chain described in `docs/architecture.md`, including the producer and playback reservation gates; the pin marker alone is no longer the full invariant.

## The stream works but banter or ads are bland

That usually means script generation failed and the app fell back to stock copy.

Chaos recovery copy follows the spoken mode too: Normal Mode uses English-led
stock, while Italian stock is used only when Super Italian Mode is enabled and
the station language is Italian. The one-host recovery line follows the same
rule.

## A host transition stopped after only a few words

Song-end transitions are validated before they reach TTS. Missing, malformed, shorter-than-three-word, or visibly cut-off text (for example `And now...`) is replaced immediately with complete stock copy; the station does not buy another model retry for this handoff. Normal Mode uses an English-led line such as `Stay close, amici — a quick word from our sponsors.` and Super Italian Mode uses the matching Italian line. Those fallbacks deliberately carry no just-played-track reference, so a queue reorder cannot make a generic handoff claim the wrong song.

Generated banter keeps lively interruptions only when the next emitted line belongs to a different host and answers or counters the cut-in. A terminal cut-off, same-speaker continuation, or stray one/two-word fragment rejects the generated exchange and uses the existing stock banter instead. This is script validation only: it does not change TTS, FFmpeg, streaming, or Home Assistant runtime behavior.

## A host answered themselves, or a break sounded shorter than it should

Individual written lines can be unusable and get dropped before air: a line that
is only a stage direction (`[ride]`, `[applausi]`) sanitizes to nothing, a model
sometimes repeats a line verbatim, and a guest-host cameo is dropped when the
guest was not invited to that break. Dropping one used to shorten the exchange
silently, and if the dropped line sat between the two hosts their surrounding
lines welded onto one speaker.

A drop is now only allowed to air when what remains still reads as a
conversation. Fewer than two surviving lines, or a drop that removed the other
host's line from between two lines by the same host, rejects the generated
exchange and uses the complete stock banter instead. A model that simply wrote
two lines for one host is left alone — that is a taste problem, not a hole, and
trading it for stock copy would lose a serviceable break. The hosts are also
asked in every banter prompt to keep stage directions out of spoken copy, so the
sanitizer has less to remove.

Check: grep the log for `Dropped empty banter line`, `Dropped duplicate banter
line`, `Dropped gated guest-host banter line`, `Dropped malformed banter line`,
and the summary line `Banter lost lines before air`. With Show Memory (the
provenance ledger) enabled, the same counts land on the segment's Tier-2 row as
`line_accounting` (`authored`, `aired`, and a per-reason breakdown); the field is
present only when lines were actually lost, so its absence means a full exchange.
Persistent losses usually mean the model is writing stage directions or repeating
itself — worth checking before assuming a TTS or audio fault, since a per-line
voice failure fails the whole segment rather than airing it short.

The app tries Anthropic first, then falls back to OpenAI through the active
quality profile if `OPENAI_API_KEY` is set (the role-specific catalog entry in
`model_registry.toml`), then to stock lines. Check the registry—not Python or
`radio.toml`—when changing a script model or its token price.
When Anthropic returns an authentication failure (for example `invalid x-api-key`) or a non-retryable provider configuration error (for example a 404/model-not-found from an invalid Claude model ID), the app suspends Anthropic for 10 minutes in-process and routes script generation to OpenAI immediately to avoid repeated provider spam. Concurrent banter, ad, and transition generations share a single attempt lock: the first call trips the circuit; sibling calls queued on the lock see the block and fall straight to OpenAI instead of each racing through their own failed request. After the 10-minute cooldown the next call logs a provider backoff expiry and makes exactly one retry; a successful retry clears the block, a fresh failure re-arms it for another 10 minutes.
A temporary overload or rate limit (HTTP 429/529) uses a separate, much shorter breaker: it benches Anthropic for a bounded cooldown (default 20s, honoring a `Retry-After` header when present, clamped to 5–60s) so affected later segments go straight to OpenAI, then retries Anthropic automatically once the cooldown expires. A 429 is scoped to the failing model; a 529 overload benches Anthropic account-wide. This path needs no operator action — `/status` reports it as a self-recovering transient state, not an auth/config block.

Check:

- `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is set (at least one is needed for AI-generated content)
- outbound network access is available
- `/status` or the dashboard shows recent producer errors
- `/api/capabilities` and `/status` now include `provider_health.anthropic` (`degraded`, `retry_after_s`, `auth_failures`)

If generated banter airs but listener memory or song callbacks are not growing,
check the post-air extractor path separately:

- The extractor runs only after generated banter sends cleanly. Canned clips,
  stock/impossible fallback copy, skipped segments, source switches, partial
  sends, and banter that no listener accepted intentionally do not write memory.
- The segment metadata must include `memory_extraction`, and the streamer must
  reach EOF with bytes sent AND at least one listener queue accepting a chunk
  before scheduling `memory_extract`. Bytes sent alone is not enough: an empty
  room still accumulates written bytes, and durable listener memory follows the
  audible boundary. If memory stops growing on a station nobody is tuned into,
  that is the gate working, not a failure.
- `memory_extract` uses the fast script role and appears in `/status`
  consumption as the Memory row (`script_memory`). Missing provider keys make it
  a warning-only no-op/fallback path, not a stream failure.
- There is no separate toggle for this extra fast-role call, and the persona
  store is initialized on normal startup. If generated banter has listener memory
  metadata and airs cleanly, extraction is attempted automatically. Leave HA
  context disabled to keep house state out of the extractor prompt; remove
  script-provider credentials to prevent durable AI memory extraction entirely.
- Persona writes require `state.persona_store`; if the store is absent,
  extraction skips before the LLM call. Song-cue writes separately require a
  pinned `youtube_id`; when it is missing, persona updates may still write but
  no song-cue row is added.

## Host voice sounds different than expected

If a host configured with `engine = "openai"` sounds like a different voice, OpenAI TTS likely failed and the host fell back to Edge TTS.

Check:

- `OPENAI_API_KEY` is set in `.env`, or in add-on mode `/config/secrets.env` (legacy add-on option fallback still works)
- Look for `Falling back to edge-tts` in logs
- `/status` may show TTS errors in the producer log

Each OpenAI host can define `edge_fallback_voice` in `radio.toml` so they fall back to their own Edge voice rather than a stranger's.

To inspect script-side OpenAI behavior (banter/ads/news/transitions/post-air memory extraction), grep logs for `openai_script_call` — every OpenAI script call emits a structured record with `model`, `caller`, `latency_ms`, `prompt_tokens`, `completion_tokens`, `json_ok`, and `fallback_reason` (one of `anthropic_absent`, `anthropic_auth_blocked`, `anthropic_auth_failed`, `anthropic_max_tokens_truncated`, `anthropic_max_tokens_truncated_retrying`, `anthropic_nonretryable`, `anthropic_transient`, `anthropic_transient_blocked`, `anthropic_usage_limit`, `anthropic_usage_limit_blocked`, `anthropic_exception`, `openai_empty_or_length`; the reason fields land in the provenance ledger / Show Memory rows — the default log format renders only the message line). A truncated Anthropic response (cut off at the token budget, partial or empty JSON) now gets ONE in-house retry at a ~1.75× budget before any provider fallback, and after a truncation-exhausted fallback OpenAI's visible-output floor inherits the escalated (not original) budget. The OpenAI side has its own single retry: a completion cut at its cap (`finish_reason="length"`, reasoning tokens starving the visible JSON) or a genuinely empty one retries once with a bigger cap — unless the model reports it finished or refused (`stop`/`content_filter`), which a bigger budget can't fix — before the stock-copy fallback. When hosts sound generic, grep the log for `truncated at max_tokens`, `retrying with escalated budget`, `escalation retry succeeded`, and the early-warning `budget pressure` WARNING (fires when a successful generation used ≥80% of its budget — raise the budget before the next truncation, don't wait for it); with the ledger enabled, the Show Memory rows carry the `fallback_reason` values above. Useful for comparing models via `OPENAI_SCRIPT_MODEL` or debugging fallback latency.

Voice validation now runs at config load, not at synthesis time:

- Every configured voice is checked against `mammamiradio/audio/voice_catalog.py` (OpenAI catalog for `engine = "openai"`, Italian edge-tts catalog for `engine = "edge"`, and the curated Azure catalog for known Azure Italian voices). Ad voices and sonic-brand sweepers can also carry their own `engine` plus `edge_fallback_voice`.
- Invalid voices are logged once as a WARNING and replaced with `it-IT-DiegoNeural` before the first synthesis attempt, so you never see repeated `Invalid voice 'onyx'` errors per segment.
- If OpenAI, Azure, or ElevenLabs is missing credentials or fails at runtime, the segment falls back to the configured Edge voice. Each cloud route carries a circuit breaker: when a route-wide failure lands (timeout, 5xx, revoked key), every waiting and later part skips straight to Edge — at most the one or two requests already in flight pay the timeout, and healthy concurrent voices on the same provider keep rendering in parallel (dialogue lines are never serialized behind each other). Transient route failures cool down for 30 seconds and then exactly one call probes the provider (a successful probe reopens the route for everyone); non-retryable credential errors stay sidelined until the route changes or the station restarts. A single bad voice ID (HTTP 400 or 404) only sidelines that one voice, not the whole provider route — other configured voices on the same Azure/ElevenLabs/OpenAI credential keep trying the cloud normally. If Edge synthesis also fails (endpoint down, throttle), the failing voice ID is memoized for the session and the next segment goes straight to the fallback voice — one attempt per voice per session, not one per segment.
- Every runtime cloud fallback now emits a route record such as `TTS fallback provider=elevenlabs ... effective_provider=edge ... reason=...` followed by `Synthesized (Edge fallback): ...`. A plain `Synthesized: ...` line means the voice was intentionally configured for Edge, not that a cloud route silently failed. Ad lines also include the configured character name.
- The admin runtime card uses those route records: `tts_provider.current_provider` becomes `edge` and `fallback_active` becomes `true` after a live cloud-to-Edge fallback, even when all provider keys are configured. This is runtime evidence, while `Mixed TTS` by itself remains a configuration summary. That runtime state is tracked per provider engine, not per voice: on a station with several voices on the same cloud engine, one voice's successful render clears the degraded state for that engine even if a different voice on the same engine is still falling back to Edge every segment. Grep logs for the specific character name in `Synthesized (Edge fallback)` lines to see which voice is actually degraded.
- When any voice was substituted at load or during live synthesis, `/api/capabilities` reports `tts_degraded: true` so the dashboard can show a degraded-TTS badge.
- If Edge fallback also fails — every configured route for that segment is down — required speech is never silenced: any partial audio is deleted, `TTSUnavailableError` is raised, and the segment falls through to the existing rescue ladder (packaged clip → norm-cache rescue → recovery sweeper → emergency tone), or for Chaos Mode banter, a canned clip. Grep logs for `all configured TTS routes are unavailable` to confirm this is what happened rather than a stuck queue.

## First Listen does not play on this device

Required First Listen proof is hearing the station in the add-on Web UI on this
device. If **Start sound check** is quiet, check mute and volume on this tab,
confirm the sound is coming from this browser and not another app, then try
**Start sound check** again. Technical details under the journey name the stream
URL. Home Assistant speakers are an optional later route, not this step.

## First Listen: the optional Home Assistant speaker route is quiet

First Listen no longer discovers or plays to speakers; it proves the station on
the device you are reading it on. The Home Assistant speaker route is optional
and lives in Home Assistant's own media browser, so it needs a working Home
Assistant connection and the HACS integration installed. Check
`/api/capabilities`: `ha: false` and `homeassistant_access: false` mean there is
no connection to send audio over.

On a standalone station (anything not run as the Home Assistant add-on), set
`HA_URL` and `HA_TOKEN` in `.env` and restart. `HA_TOKEN` is a long-lived access
token from your Home Assistant profile page. The add-on receives both from the
Supervisor and needs neither set by hand.

This step is optional. The station is a working radio without a home connection;
skipping it leaves you on the Full AI Radio tier rather than Connected Home.

For branch work, `scripts/first-listen-lab.sh start` brings up a disposable local
Home Assistant with a real speaker, so First Listen can be exercised end to end
without touching a live home. See
[docs/runbooks/first-listen-local-ha.md](runbooks/first-listen-local-ha.md).

## The station sounds soft or flat through Music Assistant

The station levels every finished segment itself, so music, hosts, beds, and ads
all reach you at one volume (`audio.lufs_target`, default `-16.0` LUFS, with ads
1 LU hotter). Music Assistant can then level the same audio a second time on its
way to a speaker.

Open the playing speaker in Music Assistant and look at the audio path. If
**Volume normalization** reads **Dynamic**, two levellers are stacked. Set it to
**fixed gain** or **disabled** for that player and play the same song again.
Dynamic levelling evens out loud and quiet moments as it goes, so drums and
plucked notes lose some of their snap and steady bass sits further forward. On
audio that arrives already levelled, there is nothing left for it to fix.

This is a Music Assistant player setting; nothing changes on the station side.
The station's own **On-Air Sound** dial is a separate FM colouring, off by
default, so it is not what you are hearing.

## Home Assistant references never show up

Check:

- `[homeassistant].enabled = true` in `radio.toml`
- `homeassistant.url` is correct
- `HA_TOKEN` is present in `.env`
- the admin Home context preview has at least one prompt-safe entity available

Even when configured correctly, HA references are opportunistic. A saved token alone stays Full AI Radio until a prompt-safe context slice exists, and the prompt only encourages one casual reference when it fits.

## Home Assistant colour is paused

Home context is projected in a separate worker process so the CPU work cannot
stall audio. Audio never depends on it: if the worker cannot run, the station
keeps playing and simply stops mentioning the house.

Two log signatures tell the two causes apart:

```
WARNING Home context projection worker could not start; Home Assistant colour is paused until it can. Audio is unaffected. Check shared memory (/dev/shm), the container's process limit, and available memory.
WARNING Home context projection worker exited; the next refresh starts a fresh one.
```

The first means the worker could not come up. It prints once per outage rather
than on every poll, and it covers three causes that surface differently
underneath: a missing, read-only, or zero-sized `/dev/shm` (Python reports this
as "named semaphores being unavailable", and it stays broken for the life of the
process once seen); a system offering too few semaphores; and an exhausted
process table or out-of-memory kernel, which fails a step later when the worker
is actually spawned. Check with `docker exec <container> df -h /dev/shm`, and
confirm the container was not started with `--shm-size=0` or an unusually low PID
limit. The second line is a worker that died mid-refresh, most often the
out-of-memory killer on a small appliance; that refresh falls back to the last
prompt-safe snapshot and the next scheduled refresh starts a new worker on its
own. A single occurrence needs no action.

## A host repeated a home detail

Open **Motore → HA context**. The fact-free **Home context rotation** row reports whether the director is waiting, has a safe cue queued, or is resting recently aired topics. Casual banter uses one allowlisted cue at most and starts a 30-minute rest when that break streams; weather flashes, rituals, and reactive directives are separate on-air lanes.

To exclude a source, mute it in the Home context preview. A mute leaves current audio alone but removes any unstarted host break that was already queued with that source. Room-presence is never routine context: it must be enabled explicitly with **Use as a personal on-air moment**, and muting it clears that permission. Turning that permission back off (or muting) also removes any unstarted presence break already queued for that sensor; audio already on air is left alone.

## Admin access

**HA add-on:** Direct LAN access to `http://<ha-ip>:8000/admin` works without any token as long as you have not configured a custom `admin_token` in the add-on options. Port 8000 serves the listener page (`/`), the admin panel (`/admin`), and the audio stream (`/stream`). From outside your home network, `/admin` returns 403.

**Standalone mode:** The app rejects non-local binds without credentials configured. Rules:

- if `ADMIN_PASSWORD` is set, admin routes require HTTP Basic auth everywhere
- if only `ADMIN_TOKEN` is set, non-local admin access requires `X-Radio-Admin-Token` header
- if neither is set, admin routes only work from localhost (or via HA add-on LAN trust)

Health probes are the exception. `/healthz` and `/readyz` stay unauthenticated so Docker, Home Assistant, and external monitors can poll them without admin credentials.

For read-only monitoring, prefer `/public-status`, `/healthz`, and `/readyz`. Do not build external monitors against `/status` or `/api/capabilities` unless you are also supplying admin auth.

## `ffmpeg` failures

Audio rendering depends on `ffmpeg` for normalization, concatenation, SFX, beds, and silence generation.

If audio generation fails, check that `ffmpeg` is installed and on `PATH`:

```bash
ffmpeg -version
```

The app logs the tail of stderr from failing ffmpeg commands, so the logs usually tell you which sub-step died.

## The music runs thin or a segment takes too long to build

If the queue is draining faster than segments are produced (you may see a
`Queue empty during active playback` bridge in the logs), find out which step is
slow before changing anything.

Every segment the producer builds logs its total build time at `INFO` (this is
wall-clock from pick to queued, so for banter/ads it also includes the script
and Home Assistant lookups, not just the audio work):

```text
Queued music in 79.2s (queue size: 2)
```

For the precise per-step audio attribution, raise the log level to `DEBUG`
(`LOG_LEVEL=DEBUG`)
for one session. Each ffmpeg stage then logs its own wall time, labelled by what
it was doing, so you can attribute the seconds:

```text
ffmpeg stage measure_lufs youtube_x.mp3: 3.10s
ffmpeg stage normalize youtube_x.mp3: 41.80s
ffmpeg stage LUFS reconcile (-4.2 dB) music_x.mp3: 31.40s
ffmpeg stage mix voice with talk bed: 34.90s
```

On the Pi these are single-threaded full-file re-encodes, so a music track that
needs both a normalize pass and a loudness-reconcile re-encode is the usual
culprit. A normalization cache hit on an already-reconciled file skips both and
should log near-instant stages.

## Tests fail during collection

If you see import errors like `ModuleNotFoundError: No module named 'dotenv'`, you are running tests outside the project env.

Use:

```bash
source .venv/bin/activate
pytest tests/
```

Or use the repo commands that now mirror CI:

```bash
make test
make check
```
