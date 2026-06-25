# Troubleshooting

This app has a lot of moving parts. Most failures reduce to three things: Python env, `ffmpeg`, or missing API keys.

## First checks

Use the expected project environment:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
./start.sh
```

If you run tests or the app from the system Python and see missing modules like `dotenv`, you are not in the repo environment.

If the dashboard is in the first-run setup flow, trust the banner. The station classifies itself as `Demo Radio`, `Full AI Radio`, or `Connected Home` based on available API keys.

Useful probe endpoints:

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
```

`/healthz` just answers "is the process alive?". `/readyz` answers "is the station actually ready to play audio right now?" and returns `starting` while startup is still warming the queue or when active listeners have hit prolonged silence.

## The app starts but there is no real music

The station walks a fallback chain at boot: charts (when `MAMMAMIRADIO_ALLOW_YTDLP=true`) → Jamendo (when `jamendo_client_id` is set) → local `music/` MP3s → bundled demo assets → built-in `DEMO_TRACKS`. The first tier that yields tracks wins. If you hear silence or placeholder tones:

- Check that `ffmpeg` is installed
- Check that `MAMMAMIRADIO_ALLOW_YTDLP=true` is set (it is by default in HA addon and Conductor)
- A quality gate circuit breaker lets tracks through after 3 consecutive rejections to prevent stream starvation

When listeners are connected, `/readyz` now also flips back to `503 starting` if playback has been silent for more than 30 seconds. The playback loop first tries a canned clip, then a recent-aware random `cache/norm_*.mp3` pick that avoids the current/recent song when alternatives exist, then, if `mammamiradio/assets/demo/music/` has any bundled MP3s, a random pick from that directory (the **built-in demo track rescue** — prevents dead air on fresh installs and empty-cache container starts, a no-op when the directory is empty), and after 60 seconds without any bridge asset it requests a forced banter segment from the producer so the queue can recover without a restart. If the station has been explicitly stopped (Stop button on the admin panel), `/readyz` returns `503 stopped` regardless of queue depth so Home Assistant Supervisor and external load balancers do not route fresh listeners to a deliberately paused station. Reconnecting a listener auto-resumes the session and clears `session_stopped` before audio begins.

The app persists the last selected source to `cache/playlist_source.json` and restores it on restart. If a persisted source fails to load, startup walks the standard fallback chain (charts → Jamendo → local `music/` → bundled demo → `DEMO_TRACKS`). Operators with MP3s in `music/` will hit tier 4 even when yt-dlp is off and Jamendo isn't configured — yt-dlp is only required to download charts, not to play files already on disk.

## A chart entry sounded like a podcast or audiobook

Apple Music's Italian chart occasionally surfaces non-music entries (BBC comedy, news briefings, audiobooks). These used to reach the queue and play as flat voice audio that broke the radio illusion. The WS5 ingest filter now rejects them before they enter the candidate pool.

Expected log signature on chart load:

```
INFO Rejecting non-music chart entry: BBC Studios - Do You Speak English? - Big Train
INFO Chart ingest: filtered 3 non-music entries
```

If a legitimate song is being rejected, check `mammamiradio/playlist/playlist.py::_NON_MUSIC_MARKERS`. The list is deliberately narrow (podcast, bbc comedy, audiobook, news briefing, asmr, …) so real titles almost never trip it. If a real Italian song title legitimately contains one of these markers, remove the marker from the list rather than loosening the check.

## The station keeps rejecting the same track

If a track fails `validate_download` (too short, corrupt, missing duration), the cached copy at `cache_dir/{cache_key}.mp3` used to stay put. The next selection of the same track returned it as a cache hit and the gate rejected it again. Endless loop.

WS5 purges the file and adds the cache key to an in-process denylist for the remainder of the session. The producer's main-loop, prefetch, and prewarm paths short-circuit on denylisted keys before calling `download_track` again.

The music quality gate (mostly silence, short normalization output) has a different escape valve — the 3-consecutive-rejection circuit breaker — and does NOT denylist source tracks. A quality-gate rejection drops the cached normalization so it's recomputed next time, but the source track can still be re-picked (the gate failure is usually a normalization artifact, not source corruption).

Expected log signature:

```
WARNING Skipping track due to invalid download (Some Artist - Some Title): duration too short (8.2s)
WARNING Purged rejected cache file abc123.mp3: duration too short (8.2s)
DEBUG Skipping denylisted track (already rejected this session): Some Artist - Some Title
```

The denylist is process-local — it clears on restart so a track that was transiently bad gets another chance after the next boot.

## A song a listener requested played twice

A listener song request was pinned to the "play next" slot from two places: once by the background download (`_commit_external_download`) when the file finished, and again by the dedication banter (`_plan_listener_request_block`) the next time a host break was produced — because the request lingers in `state.pending_requests` until that banter's deferred commit applies. Each pin is consumed by `select_next_track` *before* the repeat-cooldown filter runs, so the song aired a second time a few minutes later (the 2026-06-19 "double Linkin Park").

The fix marks the request `song_pinned` at whichever site pins first (set synchronously, so it is also safe against two banters peeking the same pending request in the lookahead window), and the dedication banter no longer re-pins an already-pinned request. A requested song now airs exactly once. If you still see a repeat, check that both pin sites consult `req["song_pinned"]` and that `select_next_track`'s pinned-track short-circuit (`core/models.py`) hasn't been changed to skip the marker.

## The stream works but banter or ads are bland

That usually means script generation failed and the app fell back to stock copy.

The app tries Anthropic first, then falls back to OpenAI through the active quality profile if `OPENAI_API_KEY` is set (`gpt-5.5` for creative copy in balanced/premium, `gpt-5.4-mini` for fast transitions), then to stock lines.
When Anthropic returns an authentication failure (for example `invalid x-api-key`) or a non-retryable provider configuration error (for example a 404/model-not-found from an invalid Claude model ID), the app suspends Anthropic for 10 minutes in-process and routes script generation to OpenAI immediately to avoid repeated provider spam. Concurrent banter, ad, and transition generations share a single attempt lock: the first call trips the circuit; sibling calls queued on the lock see the block and fall straight to OpenAI instead of each racing through their own failed request. After the 10-minute cooldown the next call logs a provider backoff expiry and makes exactly one retry; a successful retry clears the block, a fresh failure re-arms it for another 10 minutes.

Check:

- `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is set (at least one is needed for AI-generated content)
- outbound network access is available
- `/status` or the dashboard shows recent producer errors
- `/api/capabilities` and `/status` now include `provider_health.anthropic` (`degraded`, `retry_after_s`, `auth_failures`)

## Host voice sounds different than expected

If a host configured with `engine = "openai"` sounds like a different voice, OpenAI TTS likely failed and the host fell back to Edge TTS.

Check:

- `OPENAI_API_KEY` is set in `.env` (or `/config/secrets.env` in the add-on)
- Look for `Falling back to edge-tts` in logs
- `/status` may show TTS errors in the producer log

Each OpenAI host can define `edge_fallback_voice` in `radio.toml` so they fall back to their own Edge voice rather than a stranger's.

To inspect script-side OpenAI behavior (banter/ads/news/transitions), grep logs for `openai_script_call` — every OpenAI script call emits a structured record with `model`, `caller`, `latency_ms`, `prompt_tokens`, `completion_tokens`, `json_ok`, and `fallback_reason` (one of `anthropic_absent`, `anthropic_auth_blocked`, `anthropic_auth_failed`, `anthropic_max_tokens_truncated`, `anthropic_nonretryable`, `anthropic_usage_limit`, `anthropic_usage_limit_blocked`, `anthropic_exception`). `anthropic_max_tokens_truncated` means the Anthropic response was cut off at the token budget (partial or empty JSON) and the call fell back to OpenAI — grep for it to measure how often the host writer runs long. Useful for comparing models via `OPENAI_SCRIPT_MODEL` or debugging fallback latency.

Voice validation now runs at config load, not at synthesis time:

- Every configured voice is checked against `mammamiradio/audio/voice_catalog.py` (OpenAI catalog for `engine = "openai"`, Italian edge-tts catalog for `engine = "edge"`, and the curated Azure catalog for known Azure Italian voices). Ad voices and sonic-brand sweepers can also carry their own `engine` plus `edge_fallback_voice`.
- Invalid voices are logged once as a WARNING and replaced with `it-IT-DiegoNeural` before the first synthesis attempt, so you never see repeated `Invalid voice 'onyx'` errors per segment.
- If OpenAI, Azure, or ElevenLabs is missing credentials or fails at runtime, the segment falls back to the configured Edge voice. If Edge synthesis still fails (endpoint down, throttle), the failing voice ID is memoized for the session and the next segment goes straight to the fallback voice — one attempt per voice per session, not one per segment.
- When any voice was substituted at load, `/api/capabilities` reports `tts_degraded: true` so the dashboard can show a degraded-TTS badge.

## Home Assistant references never show up

Check:

- `[homeassistant].enabled = true` in `radio.toml`
- `homeassistant.url` is correct
- `HA_TOKEN` is present in `.env`

Even when configured correctly, HA references are opportunistic. The prompt only encourages one casual reference when it fits.

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
