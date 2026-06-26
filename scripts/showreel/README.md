# Showreel — staged audio snippets from the real station

The audio analog of a "release → feature GIF" pipeline. Given a chosen song, a forced
segment order, and a staged Home-Assistant scene, it drives a **local instance of the real
station** and records the **live stream** — so the output is genuine production audio
(real host voices, real loudness/processing, the producer's own transitions and ad
bumpers), captured as one continuous take. Nothing is stitched.

Use it to make a shareable ~90s snippet whenever there's something new to show: a new host,
a new section, a new mode.

## Pieces

- **`mock_ha.py`** — a tiny mock Home Assistant REST API. Serves a *staged* home scene so
  the producer genuinely derives a home mood and the hosts weave it into banter, without a
  real HA and without leaking real-home telemetry. Scenarios are plain dicts chosen so the
  real `classify_home_mood()` returns the intended mood (default `coffee` →
  "Caffè in preparazione"). Add scenarios by editing `SCENARIOS` / `FORECASTS`.
- **`capture.py`** — drives the local station: connects a warmup listener, waits for a long
  lead track, forces the segment order, records `/stream`, and auto-trims the contiguous
  arc into a final clip.

## How the back-to-back ordering works

Operator triggers (`/api/trigger`) front-insert at the play-queue HEAD ("air-next"). Firing
them in **reverse** (news, ad, banter) during a long lead track means they end up queued as
`[banter, ad, news]` and air gaplessly when the lead track ends. The lead track must be long
enough (~3 min) to cover all three generations. Then only the outer ends are trimmed — the
seams between segments are the producer's real transitions.

Caveat: the play queue is bounded (`lookahead_segments + 2`). With three forced segments
*plus* the producer's lookahead music, a front-insert can tail-drop the furthest-future
segment (this is why a 3rd segment can go missing). For a reliable 3-segment arc, purge the
lookahead music right before triggering, or keep to a 2-segment arc (music→banter→ad already
covers music/voice/interstitial).

## Run it

```bash
# 1. Local CC music (≥40s tracks; one long ~180s "lead" track for the gapless trick)
mkdir -p music && cp "scripts/showreel_assets/...mp3" "music/Artist - Title.mp3"

# 2. Stage a home scene
python scripts/showreel/mock_ha.py --port 8123 --scenario coffee &

# 3. Start the REAL station against the mock HA (local, CC music, ledger on)
MAMMAMIRADIO_BIND_HOST=127.0.0.1 MAMMAMIRADIO_PORT=8077 \
HA_ENABLED=true HA_URL=http://127.0.0.1:8123 HA_TOKEN=dummy-token \
MAMMAMIRADIO_LEDGER_ENABLED=true MAMMAMIRADIO_ALLOW_YTDLP=false JAMENDO_CLIENT_ID= \
STATION_NAME="Mamma Mi Radio" \
.venv/bin/python -m uvicorn mammamiradio.main:app --host 127.0.0.1 --port 8077 &

# 4. Capture → final continuous clip
python scripts/showreel/capture.py --base http://127.0.0.1:8077 \
  --lead-track "Night in Venice" --final scripts/showreel_out/ma-pr-3836.mp3
```

## Notes / gotchas (learned the hard way)

- **Model:** the creative role runs on the account's best *available* model. `claude-fable-5`
  is gated (404 → "use Opus 4.8"), so this used **Opus 4.8**. Setting `CLAUDE_CREATIVE_MODEL`
  to a gated model silently falls back to OpenAI (English-code-switched output) — don't.
- **Track length is load-bearing.** Local tracks are declared 210s but a forced segment only
  airs after the *current* track ends. Use short tracks (~47s) for fast airing, plus one long
  lead track for the gapless-ordering trick. A <35s track is rejected by the audio quality
  gate ("duration too short").
- **No live capture of random output** — content is staged/forced and curated. But it's the
  real engine: the home-awareness and any emergent character (see the provenance ledger,
  `MAMMAMIRADIO_LEDGER_ENABLED=true`) are genuine, not scripted.
- **Copyright:** ship only CC/CC0 music in a public sample. Ad brands must be fictional.
- Mock HA is a flaky background process; re-check `curl :8123/api/states` before a run.

## Output of the first run

`scripts/showreel_out/ma-pr-3836.mp3` — music→banter→ad, ~1:45, for Music Assistant
PR #3836. Story + lift-and-use blurbs in `scripts/showreel_out/ma-pr-3836-notes.md`.
