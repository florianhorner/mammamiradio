# Repo Map — Where Things Live

If you want to fix or extend X, look in Y. The folder hierarchy IS the mental model (leadership principle #4).

## Source code

| What you want to change                            | Where to look                                |
|----------------------------------------------------|-----------------------------------------------|
| What hosts say (banter, jokes, callouts)           | `mammamiradio/hosts/scriptwriter.py`         |
| Host voice: expression banks, fingerprints, Chaos/Festival fiction | `mammamiradio/hosts/prompt_world.py` |
| Transition rewrite openers (anti-repeat copy + helpers) | `mammamiradio/hosts/transitions.py`     |
| Stock fallback copy: chaos stock lines, ad-break bumpers | `mammamiradio/hosts/fallbacks.py`       |
| Host personality, listener memory, motifs          | `mammamiradio/hosts/persona.py`              |
| Time-of-day / cultural cues injected into prompts  | `mammamiradio/hosts/context_cues.py`         |
| Ads (brands, voices, campaign spines)              | `mammamiradio/hosts/ad_creative.py`          |
| Foreign/competitor station-name scrubbing (spoken + now-playing) | `mammamiradio/hosts/station_name_guard.py` |
| Music sources (charts, Jamendo, local files)       | `mammamiradio/playlist/playlist.py`          |
| yt-dlp / Jamendo / local file fetch                | `mammamiradio/playlist/downloader.py`        |
| Per-track rules ("skip the bridge", anthems)       | `mammamiradio/playlist/track_rules.py`       |
| Per-track machine-derived song memory              | `mammamiradio/playlist/song_cues.py`         |
| "Why this track?" rationale generation             | `mammamiradio/playlist/track_rationale.py`   |
| FFmpeg normalize / mix / concat / SFX              | `mammamiradio/audio/normalizer.py`           |
| Station imaging stingers and talk beds             | `mammamiradio/audio/imaging.py`              |
| Edge / OpenAI / Azure / ElevenLabs TTS synthesis   | `mammamiradio/audio/tts.py`                  |
| Audio quality gate (duration, silence checks)      | `mammamiradio/audio/audio_quality.py`        |
| Voice catalog (Edge, OpenAI, Azure voice IDs)      | `mammamiradio/audio/voice_catalog.py`        |
| Generate TTS audition clips and manifest           | `scripts/audition_tts_voices.py`            |
| Home Assistant polling / state formatting          | `mammamiradio/home/ha_context.py`            |
| HA event derivation (diffs, pruning)               | `mammamiradio/home/ha_enrichment.py`         |
| Segment scheduling (banter / ad / music)           | `mammamiradio/scheduling/scheduler.py`       |
| Producer loop (queue ahead of playback)            | `mammamiradio/scheduling/producer.py`        |
| WTF clip extraction + ring buffer                  | `mammamiradio/scheduling/clip.py`            |
| Party mode toggle (Festival Mode, future themes)   | `mammamiradio/web/streamer.py` + `docs/party-mode-extension.md` |
| HTTP routes / playback loop                        | `mammamiradio/web/streamer.py`               |
| Admin auth (credentials, CSRF, trusted networks)   | `mammamiradio/web/auth.py`                   |
| Listener-request endpoints (dedica, song wish)     | `mammamiradio/web/listener_requests.py`      |
| Open Graph share card                              | `mammamiradio/web/og_card.py`                |
| Listener / admin / clip HTML                       | `mammamiradio/web/templates/`                |
| CSS / JS / icons / service worker                  | `mammamiradio/web/static/`                   |
| `radio.toml` parsing + `.env`                      | `mammamiradio/core/config.py`                |
| Shared data models (Track, Segment, etc.)          | `mammamiradio/core/models.py`                |
| Capability flags + tier derivation                 | `mammamiradio/core/capabilities.py`          |
| Legacy setup-status classification                 | `mammamiradio/core/setup_status.py`          |
| SQLite schema / migrations                         | `mammamiradio/core/sync.py`                  |
| App startup / shutdown lifecycle                   | `mammamiradio/main.py`                       |
| Demo MP3s / SFX / studio bleeds / logo             | `mammamiradio/assets/`                       |
| HACS/Home Assistant integration                    | `custom_components/mammamiradio/`            |
| Home Assistant add-on packaging                    | `ha-addon/mammamiradio/` + `ha-addon/mammamiradio-edge/` |

## Tests

The `tests/` tree mirrors the source tree exactly. To find the test for `mammamiradio/hosts/persona.py`, look in `tests/hosts/test_persona.py`.

| Source nave              | Test dir              |
|---------------------------|-----------------------|
| `mammamiradio/core/`     | `tests/core/`         |
| `mammamiradio/audio/`    | `tests/audio/`        |
| `mammamiradio/playlist/` | `tests/playlist/`     |
| `mammamiradio/hosts/`    | `tests/hosts/`        |
| `mammamiradio/home/`     | `tests/home/`         |
| `mammamiradio/scheduling/` | `tests/scheduling/` |
| `mammamiradio/web/`      | `tests/web/`          |
| HA addon packaging       | `tests/addon/`        |
| Repo scripts / lifecycle | `tests/repo/`         |
| CI workflow contract     | `tests/workflows/`    |

## Docs

| Doc                              | Path                            |
|----------------------------------|----------------------------------|
| Product pitch                    | `README.md`                      |
| Local setup, conventions         | `CONTRIBUTING.md`                |
| Agent rules + leadership         | `CLAUDE.md`                      |
| Release notes                    | `CHANGELOG.md`                   |
| Runtime flow + API routes        | `docs/architecture.md`           |
| Deploy / production reality      | `docs/operations.md`             |
| Common failures + recovery       | `docs/troubleshooting.md`        |
| HA addon release process         | `docs/runbooks/ha-addon.md`      |
| HACS/Home Assistant integration  | `docs/integrations/ha-integration.md` |
| HA privacy + upstream proposals  | `docs/integrations/ha-privacy-and-upstream-proposals.md` |
| Festival Mode (operator guide)   | `docs/festival-mode.md`          |
| Adding a new party mode theme    | `docs/party-mode-extension.md`   |
| Design system (colors, fonts)    | `docs/design/system.md`          |
| Admin panel layout standards     | `docs/design/admin-panel.md`     |
| Conductor workspace lifecycle    | `docs/conductor.md`              |
| Listener QS integration train    | `docs/listener-qs-train.md`      |
| Cathedral restructure plan       | `docs/archive/2026-04-28-cathedral-restructure.md` |

## God modules pending split

Two modules carry a `# TODO: split` marker referencing the cathedral plan
(`docs/archive/2026-04-28-cathedral-restructure.md`, PRs 5 & 6, deferred after "the
cathedral has walls"):

- `mammamiradio/web/streamer.py` (~4,000 LOC)
- `mammamiradio/hosts/scriptwriter.py` (~2,200 LOC)

**Status — probe-first, NOT a committed program (decided 2026-06-20).** This is real
but *unbitten* debt, so it is gated on two cheap probes before any multi-cut program is
committed. No cadence or floor; remaining cuts defer behind a named tripwire (a feature
demonstrably harder because of a god module, a token-burn event, or a positive
degradation probe).

1. **Degradation probe** — does an agent edit a **core-touching** `scriptwriter` task
   (the Anthropic→OpenAI fallback chain, `_cached_system_prompt`, or the `max_tokens`/
   GPT-5 path) measurably better against a split than the monolith? A leaf-only task is
   not valid: host behavior already lives in the extracted leaves and never opens the
   core. Positive → `scriptwriter` jumps the queue as product work.
2. **Cost probe** — ship the narrowed first cut (below) and measure what it actually costs.

**The cut menu (parked; the probes size it):**

`streamer.py` → 4 cuts, in order:
1. `status_payload.py` — the **pure serialize/payload leaf only**: `_serialize_*`,
   `_paginated_tracks`, `_status_now_playback`, `_page_bounds`, `_golden_path_status`
   (+TTL), `_cached_cache_size_mb`, `_ha_details_payload`. **Must NOT move** in this cut:
   `_public_status_payload`, the `_*_snapshot` family, or the live-clock cluster
   (`_runtime_monotonic`/`_queue_empty_elapsed`/`_silence_with_listeners`) — the playback
   loop and `/healthz`+`/readyz` call them, so moving them risks an unplanned addon
   restart. They wait for a later `runtime_health.py` leaf.
2. `playback_loop.py` — `LiveStreamHub`, `run_playback_loop`, `_purge_queue_and_shadow`,
   the live-clock cluster, norm-cache rescue (also fix the hardcoded paths in
   `scripts/check-release-invariants.sh` in the same cut).
3. `routes_listener.py` — the 12 listener routes (own `APIRouter`, combined in the facade).
4. `routes_admin.py` — the ~45 admin routes; `streamer.py` becomes a thin facade.

`scriptwriter.py` → 5 cuts, leaf-first: `script_shared.py` → `prompts.py` → `llm_client.py`
→ `ads.py` → `banter.py` (facade last). Each cut updates the `hot_reload_modules` reload
chain leaves-first in lockstep. Data leaves `prompt_world.py`, `transitions.py`,
`fallbacks.py`, `station_name_guard.py` are already extracted.

Every cut is behavior-preserving and byte-faithful: facade re-export + identity-guard
test, whole-repo patch-string grep, per-module coverage floor, edge-soak on the Pi
(`/public-status`, `/status`, `/healthz`, `/readyz`, first `/stream` byte). Full per-cut
discipline: `docs/runbooks/refactor-cuts.md`.

Until the probes run and a cut lands, these modules are postal addresses, not
destinations. Ride the structure that exists today; do not pre-split.
