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
  "Caffè in preparazione"; `homecoming` stages the front door for the unlock moment).
  Add scenarios by editing `SCENARIOS` / `FORECASTS`. States are **mutable at runtime**
  via `POST /__set {"entity_id": ..., "state": ...}` — the station derives events by
  diffing consecutive polls, so a reactive trigger (door unlock → "bentornato") only
  fires when a capture stages the *transition*, not just the end state.
- **`capture.py`** — drives the local station: connects a warmup listener, waits for a long
  lead track, forces the segment order, records `/stream`, and auto-trims the contiguous
  arc into a final clip. The arc is configurable (`--arc banter` for a single-segment
  moment; default `banter,ad,news_flash`). Home-event mode is self-priming: it captures
  the baseline, consumes the listener moment, stages a real state transition, waits for
  the configured HA context TTL, then records the final arc. A failed precondition exits
  without creating or replacing a final clip.

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
MAMMAMIRADIO_HA_CONTEXT_POLL_INTERVAL=15 \
HA_ENABLED=true HA_URL=http://127.0.0.1:8123 HA_TOKEN=dummy-token \
MAMMAMIRADIO_LEDGER_ENABLED=true MAMMAMIRADIO_ALLOW_YTDLP=false JAMENDO_CLIENT_ID= \
STATION_NAME="Mamma Mi Radio" \
.venv/bin/python -m uvicorn mammamiradio.main:app --host 127.0.0.1 --port 8077 &

# 4. Capture → final continuous clip
python scripts/showreel/capture.py --base http://127.0.0.1:8077 \
  --lead-track "Night in Venice" --final scripts/showreel_out/ma-pr-3836.mp3
```

## Staged home-event capture (reactive-trigger moments)

For moments driven by a `REACTIVE_TRIGGERS` directive (door unlock, coffee machine
switching on, a person arriving) the entity has to *change state between two polls* —
serving the end state from boot produces no event. Flow:

```bash
python scripts/showreel/mock_ha.py --port 8123 --scenario homecoming &
# Start the station as above. Its command sets the 15s context TTL required here.
python scripts/showreel/capture.py --base http://127.0.0.1:8077 \
  --lead-track "Night in Venice" --arc banter \
  --home-event lock.lock_ultra_8d3c:unlocked --mock-ha http://127.0.0.1:8123 \
  --ha-poll-interval 15 \
  --final scripts/showreel_out/door-bentornato.mp3
```

The tool catches the lead before it starts recording, then queues an unrecorded
`news_flash` baseline and an unrecorded `banter` to consume the warmup listener moment. It
waits for each requested operator render to clear before advancing, so an older automatic
banter cannot be mistaken for the preflight take. Each recorded segment is then tracked by
its queue ID, so a same-type preflight take immediately after the requested arc cannot
extend the final trim. It flips the mock state, waits
`--ha-poll-interval + 1s`, starts recording, and queues the requested arc. `--home-event`
requires an arc that starts with `banter`; this guarantees the fresh directive belongs to
the first captured host break. The first arc wait is bounded by `--first-wait` (default
300s), which must cover the remainder of the lead track.

`--ha-poll-interval` must match `MAMMAMIRADIO_HA_CONTEXT_POLL_INTERVAL`. It defaults to
`15`, matching this recipe. Use a larger value for a station started with a larger TTL.

## Running the creative role on a specific model

`CLAUDE_CREATIVE_MODEL=claude-fable-5` (env override, documented in the root `CLAUDE.md`)
swaps the creative model for the run without touching profiles. **Probe first**: force one
banter and confirm the ledger row (`cache/ledger/`) names that model as the generator — a
gated model silently falls back to OpenAI (English-code-switched output), which is how the
first showreel ended up on Opus 4.8. Run that probe in a short, separate local station
session. A probe deliberately leaves a forced banter queued; stop the probe pair after the
ledger check, then start a fresh mock/station pair for the lead-track capture.

## Notes / gotchas (learned the hard way)

- **Model:** the creative role runs on the account's best *available* model. On the first
  run `claude-fable-5` was gated (404 → "use Opus 4.8"), so it used **Opus 4.8**. See the
  probe-first section above before trusting any model override.
- **Track length is load-bearing.** Local tracks are declared 210s but a forced segment only
  airs after the *current* track ends. Use short tracks (~47s) for fast airing, plus one long
  lead track for the gapless-ordering trick. A <35s track is rejected by the audio quality
  gate ("duration too short").
- **No live capture of random output** — content is staged/forced and curated. But it's the
  real engine: the home-awareness and any emergent character (see the provenance ledger,
  `MAMMAMIRADIO_LEDGER_ENABLED=true`) are genuine, not scripted.
- **Copyright:** ship only CC/CC0 music in a public sample. Ad brands must be fictional.
- Mock HA is a flaky background process; re-check `curl :8123/api/states` before a run.
- **Local only:** the mock and station bind to `127.0.0.1`. This harness must never target
  Home Assistant Green or a live station.
- **Cleanup:** save the mock and station PIDs, then stop both after the capture. Commit only
  clips that pass the ledger check and a continuity listen, with notes beside the MP3.

## Output of the first run

`scripts/showreel_out/ma-pr-3836.mp3` — music→banter→ad, ~1:45, for Music Assistant
PR #3836. Story + lift-and-use blurbs in `scripts/showreel_out/ma-pr-3836-notes.md`.
