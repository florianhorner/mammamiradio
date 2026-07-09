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
  moment; default `banter,ad,news_flash`), and `--home-event ENTITY:STATE` +
  `--mock-ha URL` flips a staged entity right after the lead track is caught so the
  reactive directive rides into the banter generated next (`--event-settle` seconds later).

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

## Staged home-event capture (reactive-trigger moments)

For moments driven by a `REACTIVE_TRIGGERS` directive (door unlock, coffee machine
switching on, a person arriving) the entity has to *change state between two polls* —
serving the end state from boot produces no event. Flow:

```bash
python scripts/showreel/mock_ha.py --port 8123 --scenario homecoming &
# start the station as above, plus a fast poll so the flip lands quickly:
#   MAMMAMIRADIO_HA_CONTEXT_POLL_INTERVAL=15
python scripts/showreel/capture.py --base http://127.0.0.1:8077 \
  --lead-track "Night in Venice" --arc banter \
  --home-event lock.lock_ultra_8d3c:unlocked --mock-ha http://127.0.0.1:8123 \
  --final scripts/showreel_out/door-bentornato.mp3
```

The reactive event window is 2 minutes — the flip fires after the lead track is caught,
so the banter generated moments later refreshes home context, diffs the transition, and
carries the directive. The wait for the first arc segment is bounded by `--first-wait`
(default 300s) — it must cover the REMAINDER of the lead track after the triggers fire,
so raise it if your lead track runs longer than ~4.5 minutes.

Two constraints, learned the hard way:

- **The station snapshots home state lazily.** There is no background poll:
  `poll_interval` is a TTL and a fetch only happens when a banter/ad/news generation
  refreshes context. Events come from diffing the new fetch against the LAST snapshot —
  so the entity's *baseline* state must have been seen by a refresh before the capture
  flips it. If the last snapshot already shows the trigger state (e.g. from a previous
  run), the flip diffs as no-change and no event fires. Reset the entity to its baseline,
  then trigger one throwaway segment (news works) so a refresh snapshots the baseline,
  THEN run the capture.
- **One pending directive at a time.** `state.ha_pending_directive` is a single slot and
  reactive triggers are only computed when it's empty. A fresh warmup listener can arm
  the new-listener moment first and the door directive never gets computed. Burn off any
  pending moment (one banter with a listener connected) before the recorded arc.

## Running the creative role on a specific model

`CLAUDE_CREATIVE_MODEL=claude-fable-5` (env > catalog, documented in the root CLAUDE.md)
swaps the creative model for the run without touching profiles. **Probe first**: force one
banter and confirm the ledger row (`cache/ledger/`) names that model as the generator — a
gated model silently falls back to OpenAI (English-code-switched output), which is how the
first showreel ended up on Opus 4.8.

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

## Output of the first run

`scripts/showreel_out/ma-pr-3836.mp3` — music→banter→ad, ~1:45, for Music Assistant
PR #3836. Story + lift-and-use blurbs in `scripts/showreel_out/ma-pr-3836-notes.md`.
