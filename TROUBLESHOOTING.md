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
