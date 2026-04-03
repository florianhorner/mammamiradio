# Troubleshooting

This app has a lot of moving parts. Most failures reduce to five things: Python env, `ffmpeg`, Spotify auth, go-librespot device state, or missing secrets.

## First checks

Use the expected project environment:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
./start.sh
```

If you run tests or the app from the system Python and see missing modules like `dotenv`, you are not in the repo environment.

If the dashboard is in the first-run setup flow, trust the banner. The station now classifies itself as `Real Spotify Mode`, `Demo Mode`, or `Degraded` instead of pretending startup is fine.

Useful probe endpoints:

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
```

`/healthz` just answers "is the process alive?". `/readyz` answers "has the queue filled enough to stream yet?" and returns `starting` until at least one segment is ready.

## The app starts but there is no real music

Possible causes:

- Spotify credentials are missing
- the `mammamiradio` playback device is not selected
- go-librespot did not start cleanly

What the app does:

- if Spotify credentials are missing, it uses the demo playlist
- if Spotify capture is unavailable, it falls back to local files, then `yt-dlp`, then placeholder tones

Check:

```bash
cat .env
tail -n 50 tmp/go-librespot.log
```

Then open Spotify and explicitly select the `mammamiradio` device.

If you want the same checks the dashboard runs, hit the admin setup probe:

```bash
curl --user "$ADMIN_USERNAME:$ADMIN_PASSWORD" http://127.0.0.1:8000/api/setup/status
curl --user "$ADMIN_USERNAME:$ADMIN_PASSWORD" -X POST http://127.0.0.1:8000/api/setup/recheck
```

If you use token auth instead of basic auth, send `X-Radio-Admin-Token`.

## Spotify device does not appear

`spotify_auth.py` uses a local callback at `http://127.0.0.1:8888/callback` and requests playback-control scopes. If the OAuth flow never completed, device transfer will not work.

Check:

- `SPOTIFY_CLIENT_ID`
- `SPOTIFY_CLIENT_SECRET`
- browser popups are not blocked for the auth flow

If the device still does not appear, restart with:

```bash
./start.sh
```

Then inspect:

```bash
tail -n 100 tmp/go-librespot.log
```

On Apple Silicon or stripped `PATH` environments, the setup checker now also searches common binary locations like `/opt/homebrew/bin/go-librespot`. If the dashboard still says the binary is missing, that path is probably wrong or the file is not executable.

## `cat /data/go-librespot/config.yml` says "No such file or directory"

That path only exists in Home Assistant add-on mode, inside the add-on container.

If you are running the app locally on macOS or Linux, use:

```bash
cat go-librespot/config.yml
```

If you are not sure which path the app is using, inspect the resolved runtime config:

```bash
.venv/bin/python -m mammamiradio.config runtime-json
```

## Tracks skip instantly on macOS

This is usually the FIFO problem.

go-librespot writes PCM into `/tmp/mammamiradio.pcm`. If nothing is reading from that FIFO, macOS throws `ENXIO` and playback skips.

This repo works around that in two places:

- `start.sh` starts a fallback `cat` drain process
- `spotify_player.py` starts a persistent drain thread in the app

If you bypass `./start.sh`, you lose part of that protection. Use the script.

## The stream works but banter or ads are bland

That usually means Claude generation failed and the app fell back to stock copy.

Check:

- `ANTHROPIC_API_KEY` is set
- outbound network access is available
- `/status` or the dashboard shows recent producer errors

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
