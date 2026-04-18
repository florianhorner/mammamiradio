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

The station uses live Italian charts when `MAMMAMIRADIO_ALLOW_YTDLP=true`, otherwise the built-in demo playlist. If you hear silence or placeholder tones:

- Check that `ffmpeg` is installed
- Check that `MAMMAMIRADIO_ALLOW_YTDLP=true` is set (it is by default in HA addon and Conductor)
- A quality gate circuit breaker lets tracks through after 3 consecutive rejections to prevent stream starvation

When listeners are connected, `/readyz` now also flips back to `503 starting` if playback has been silent for more than 30 seconds. The playback loop first tries a canned clip, then the oldest `cache/norm_*.mp3`, then, if `mammamiradio/demo_assets/music/` has any bundled MP3s, a random pick from that directory (the **WS2 demo-asset rescue** — prevents dead air on fresh installs and empty-cache container starts, a no-op when the directory is empty), and after 60 seconds without any bridge asset it requests a forced banter segment from the producer so the queue can recover without a restart. If the station has been explicitly stopped (Stop button on the admin panel), `/readyz` returns `503 stopped` regardless of queue depth so Home Assistant Supervisor and external load balancers do not route fresh listeners to a deliberately paused station. Reconnecting a listener auto-resumes the session and clears `session_stopped` before audio begins.

The app persists the last selected source to `cache/playlist_source.json` and restores it on restart. If a persisted source fails to load, startup falls back to charts then demo tracks.

## The stream works but banter or ads are bland

That usually means script generation failed and the app fell back to stock copy.

The app tries Anthropic first, then falls back to OpenAI `gpt-4o-mini` if `OPENAI_API_KEY` is set, then to stock lines.
When Anthropic returns an authentication failure (for example `invalid x-api-key`), the app suspends Anthropic for 10 minutes in-process and routes script generation to OpenAI immediately to avoid repeated 401 spam. Concurrent banter, ad, and transition generations share a single attempt lock: the first call trips the circuit; sibling calls queued on the lock see the block and fall straight to OpenAI instead of each racing through their own 401. After the 10-minute cooldown the next call logs `Anthropic auth backoff expired; retrying Anthropic after cooldown` and makes exactly one retry; a successful retry clears the block, a fresh 401 re-arms it for another 10 minutes.

Check:

- `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is set (at least one is needed for AI-generated content)
- outbound network access is available
- `/status` or the dashboard shows recent producer errors
- `/api/capabilities` and `/status` now include `provider_health.anthropic` (`degraded`, `retry_after_s`, `auth_failures`)

## Host voice sounds different than expected

If a host configured with `engine = "openai"` sounds like a different voice, OpenAI TTS likely failed and the host fell back to Edge TTS.

Check:

- `OPENAI_API_KEY` is set in `.env` (or addon options)
- Look for `Falling back to edge-tts` in logs
- `/status` may show TTS errors in the producer log

Each OpenAI host can define `edge_fallback_voice` in `radio.toml` so they fall back to their own Edge voice rather than a stranger's.

Voice validation now runs at config load, not at synthesis time:

- Every configured voice is checked against `mammamiradio/voice_catalog.py` (OpenAI catalog for `engine = "openai"` hosts, Italian edge-tts catalog for `engine = "edge"` hosts and all ad voices).
- Invalid voices are logged once as a WARNING and replaced with `it-IT-DiegoNeural` before the first synthesis attempt, so you never see repeated `Invalid voice 'onyx'` errors per segment.
- If a runtime synthesis still fails (edge-tts endpoint down, throttle), the failing voice ID is memoized for the session and the next segment goes straight to the fallback voice — one attempt per voice per session, not one per segment.
- When any voice was substituted at load, `/api/capabilities` reports `tts_degraded: true` so the dashboard can show a degraded-TTS badge.

## Home Assistant references never show up

Check:

- `[homeassistant].enabled = true` in `radio.toml`
- `homeassistant.url` is correct
- `HA_TOKEN` is present in `.env`

Even when configured correctly, HA references are opportunistic. The prompt only encourages one casual reference when it fits.

## Remote admin access does not work

The app rejects non-local binds without auth.

Rules:

- if `ADMIN_PASSWORD` is set, admin routes require HTTP Basic auth everywhere
- if only `ADMIN_TOKEN` is set, non-local admin access requires `X-Radio-Admin-Token` header
- if neither is set, admin routes only work from localhost

Health probes are the exception. `/healthz` and `/readyz` stay unauthenticated so Docker, Home Assistant, and external monitors can poll them without admin credentials.

For read-only monitoring, prefer `/public-status`, `/healthz`, and `/readyz`. Do not build external monitors against `/status` or `/api/capabilities` unless you are also supplying admin auth.

## `ffmpeg` failures

Audio rendering depends on `ffmpeg` for normalization, concatenation, SFX, beds, and silence generation.

If audio generation fails, check that `ffmpeg` is installed and on `PATH`:

```bash
ffmpeg -version
```

The app logs the tail of stderr from failing ffmpeg commands, so the logs usually tell you which sub-step died.

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
