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

## The app starts but there is no real music

The station walks a source chain at boot: charts (when `MAMMAMIRADIO_ALLOW_YTDLP=true`) → Jamendo (when `jamendo_client_id` is set) → packaged demo music when present → built-in demo-track metadata. A source checkout also checks its repo-local `music/` directory before the demo tiers; the stock Docker Compose and Home Assistant packages do not mount that development path. The current package contains recovery audio but no bundled song library, and built-in demo-track metadata still needs a working download path. The first tier that yields playable tracks wins. If you hear only recovery audio or placeholder tones:

- Check that `ffmpeg` is installed
- Check that `MAMMAMIRADIO_ALLOW_YTDLP=true` is set (it is by default in HA addon and Conductor)
- A quality gate circuit breaker lets tracks through after 3 consecutive rejections to prevent stream starvation

When listeners are connected, `/readyz` now also flips back to `503 starting` if playback has been truly silent for more than 30 seconds — silent means no listener queue accepted audio, not merely that a file was selected. A station bridging an empty queue on `continuity_1.mp3` is audibly on air and does not count as silent, so the add-on watchdog is not handed a reason to restart a fresh install mid-first-render. The playback loop first tries one canned clip for the empty-queue gap, then a recent-aware random `cache/norm_*.mp3` pick that prefers a song the listener has not just heard, and only re-serves a recent one when the cache holds nothing else (a song from twenty minutes ago beats the station ident on a loop), then, if `mammamiradio/assets/demo/music/` has any bundled MP3s, a random pick from that directory (the **built-in demo track rescue** — prevents dead air on fresh installs and empty-cache container starts, a no-op when the directory is empty). If there is no cached or demo music, the packaged clip may repeat; the neutral two-second `emergency_tone.mp3` remains the final packaged rung when ordinary recovery cannot supply audio. After 60 seconds without any bridge asset the station requests forced banter so the queue can recover without a restart. If the station has been explicitly stopped (Stop button on the admin panel), `/readyz` returns `503 stopped` regardless of queue depth. Connecting or reconnecting to `/stream` does not clear the persisted stop; press **Resume** explicitly.

## The same short host line loops every few seconds after Resume or a queue drain

This means the station is living on continuity audio while the producer is still rendering the next segment. Current builds reach for cached music first: on a warm cache, Resume, idle wake-up, and an active-playback drain queue a normalized cached song with no clip in front of it, so the healthy path in the logs is a queued `norm-cache bridge` on its own. The packaged clip appears only when the cache has nothing eligible, and when it does queue with runway still expected but no cache music behind it, the miss reads `no cache music queued behind the canned clip`. Either way you should not see the same `continuity_1.mp3` line every few seconds.

If the clip still repeats, check whether `cache/` has any `norm_*.mp3` files, and whether the ones that exist are eligible to air: files rejected by the audio quality gate and songs on the operator ban list are skipped by the rescue picker even though they sit on disk (a missing or corrupt metadata sidecar does NOT disqualify a file — it airs with a cleaned-up filename as the title). A fresh install with no cache and no bundled demo music may legitimately repeat the packaged clip because repeated station audio is still better than dead air.

The app persists the last selected source to `cache/playlist_source.json` and restores it on restart. If a persisted source fails to load, startup walks the standard fallback chain. Source-checkout developers with MP3s in the repo-local `music/` directory can use them even when yt-dlp is off and Jamendo isn't configured — yt-dlp is only required to download charts, not to play files already on disk.

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

## A chart entry sounded like a podcast or audiobook

Apple Music's Italian chart occasionally surfaces non-music entries (BBC comedy, news briefings, audiobooks). The source filter rejects them before they enter the candidate pool.

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

The fix marks the request `song_pinned` at whichever site pins first (set synchronously, so it is also safe against two banters peeking the same pending request in the lookahead window), and the dedication banter no longer re-pins an already-pinned request. A requested song now airs exactly once. If you still see a repeat, check that both pin sites consult `req["song_pinned"]` and that `select_next_track`'s pinned-track short-circuit (`core/models.py`) hasn't been changed to skip the marker.

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

## Home Assistant references never show up

Check:

- `[homeassistant].enabled = true` in `radio.toml`
- `homeassistant.url` is correct
- `HA_TOKEN` is present in `.env`
- the admin Home context preview has at least one prompt-safe entity available

Even when configured correctly, HA references are opportunistic. A saved token alone stays Full AI Radio until a prompt-safe context slice exists, and the prompt only encourages one casual reference when it fits.

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
