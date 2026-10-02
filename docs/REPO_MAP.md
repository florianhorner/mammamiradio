# Repo Map — Where Things Live

If you want to fix or extend X, look in Y. The folder hierarchy IS the mental model (leadership principle #4).

## Source code

| What you want to change                            | Where to look                                |
|----------------------------------------------------|-----------------------------------------------|
| What hosts say (banter, jokes, callouts)           | `mammamiradio/hosts/scriptwriter.py`         |
| Host voice: expression banks, fingerprints, shape/lore data, Chaos/Festival fiction | `mammamiradio/hosts/prompt_world.py` |
| Roster-aware banter shape/lore selection and recency | `mammamiradio/hosts/relationship.py` |
| Transition rewrite openers (anti-repeat copy + helpers) | `mammamiradio/hosts/transitions.py`     |
| Stock fallback copy: chaos stock lines, ad-break bumpers | `mammamiradio/hosts/fallbacks.py`       |
| Host personality, listener memory, motifs          | `mammamiradio/hosts/persona.py`              |
| Time-of-day / cultural cues injected into prompts  | `mammamiradio/hosts/context_cues.py`         |
| Ads (brands, voices, campaign spines)              | `mammamiradio/hosts/ad_creative.py`          |
| Foreign/competitor station-name scrubbing (spoken + now-playing) | `mammamiradio/hosts/station_name_guard.py` |
| Post-air listener/song memory extraction          | `mammamiradio/hosts/memory_extractor.py`   |
| Durable base sources (starter, local, optional standalone external) | `mammamiradio/playlist/playlist.py` |
| Transient Jamendo stream lease and artifact       | `mammamiradio/playlist/jamendo_transient.py` |
| Optional standalone extraction / local normalization | `mammamiradio/playlist/downloader.py`     |
| Per-track rules ("skip the bridge", anthems)       | `mammamiradio/playlist/track_rules.py`       |
| Per-track machine-derived song memory              | `mammamiradio/playlist/song_cues.py`         |
| "Why this track?" rationale generation             | `mammamiradio/playlist/track_rationale.py`   |
| FFmpeg normalize / mix / concat / SFX              | `mammamiradio/audio/normalizer.py`           |
| Generated ad/imaging layer cache                   | `mammamiradio/audio/synth_cache.py`          |
| Station imaging stingers, beds, and recipe resolver | `mammamiradio/audio/imaging.py`             |
| Ad-recipe schema contract                          | `mammamiradio/audio/imaging_schema.py`       |
| Edge / OpenAI / Azure / ElevenLabs TTS synthesis   | `mammamiradio/audio/tts.py`                  |
| Audio quality gate (duration, silence checks)      | `mammamiradio/audio/audio_quality.py`        |
| Voice catalog (Edge, OpenAI, Azure voice IDs)      | `mammamiradio/audio/voice_catalog.py`        |
| Generate TTS audition clips and manifest           | `scripts/audition_tts_voices.py`            |
| Home Assistant polling / state formatting          | `mammamiradio/home/ha_context.py`            |
| Detached preview value (useful / ambient / empty)  | `mammamiradio/home/context_value.py`         |
| HA speaker discovery + Media Source playback       | `mammamiradio/home/ha_playback.py`           |
| Install-scoped Home authorization projection       | `mammamiradio/home/authorization.py`         |
| Legacy Home install-origin + provenance bridge     | `mammamiradio/home/migration.py`             |
| Home Context Director selection / lifecycle        | `mammamiradio/home/context_director.py`      |
| HA event derivation (diffs, pruning)               | `mammamiradio/home/ha_enrichment.py`         |
| Segment scheduling (banter / ad / music)           | `mammamiradio/scheduling/scheduler.py`       |
| Producer loop (queue ahead of playback)            | `mammamiradio/scheduling/producer.py`        |
| Atomic queued-segment drop/accounting boundary     | `mammamiradio/scheduling/queue_mutations.py` |
| WTF clip extraction + ring buffer                  | `mammamiradio/scheduling/clip.py`            |
| Post-update cold-open campaign (release beat)      | `mammamiradio/release_campaign.py`           |
| Post-restart music continuity spool                | `mammamiradio/restart_handoff.py`            |
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
| Canonical guided setup + First Listen projection   | `mammamiradio/core/setup_status.py`          |
| First Listen receipt + install-origin witnesses    | `mammamiradio/core/first_listen.py`          |
| First Listen packaged mini-show eligibility        | `mammamiradio/core/first_listen_show.py`     |
| SQLite schema / migrations                         | `mammamiradio/core/sync.py`                  |
| App startup / shutdown lifecycle                   | `mammamiradio/main.py`                       |
| Demo MP3s / SFX / studio bleeds / logo             | `mammamiradio/assets/`                       |
| HACS/Home Assistant integration                    | `custom_components/mammamiradio/`            |
| Home Assistant add-on packaging                    | `ha-addon/mammamiradio/` + `ha-addon/mammamiradio-edge/` |
| Apps store listing (blurb, intro, icon, logo)      | each app folder's `config.yaml` + `README.md` + `icon.png` / `logo.png` |
| Disposable local HA + VLC speaker lab              | `scripts/first-listen-lab.sh`                |

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
| Package-root modules (e.g. `release_campaign.py`, `restart_handoff.py`) | `tests/test_*.py` |

## Docs

| Doc                              | Path                            |
|----------------------------------|----------------------------------|
| Product pitch                    | `README.md`                      |
| Local setup, conventions         | `CONTRIBUTING.md`                |
| Agent rules + leadership         | `CLAUDE.md`                      |
| Release notes                    | `CHANGELOG.md`                   |
| Product and market status        | `docs/status-quo.md`             |
| Runtime flow + API routes        | `docs/architecture.md`           |
| Deploy / production reality      | `docs/operations.md`             |
| Common failures + recovery       | `docs/troubleshooting.md`        |
| HA addon release process         | `docs/runbooks/ha-addon.md`      |
| Which file paints which store pixel | `docs/runbooks/ha-addon.md` (Store listing) |
| Disposable First Listen HA lab   | `docs/runbooks/first-listen-local-ha.md` |
| HACS/Home Assistant integration  | `docs/integrations/ha-integration.md` |
| Festival Mode (operator guide)   | `docs/festival-mode.md`          |
| Adding a new party mode theme    | `docs/party-mode-extension.md`   |
| Design system (colors, fonts)    | `docs/design/system.md`          |
| Admin panel layout standards     | `docs/design/admin-panel.md`     |
| Conductor workspace lifecycle    | `docs/conductor.md`              |
| Parallel workspaces + landing    | `docs/runbooks/parallel-workspaces.md` |
| Listener QS integration train    | `docs/listener-qs-train.md`      |

## Runtime ownership

- `web/streamer.py` owns the live stream, playback loop and HTTP routes.
- `web/status_payload.py` owns shared status serialization and diagnostics;
  `web/auth.py` owns admin authentication and request protection.
- `hosts/scriptwriter.py` assembles scripts from the prompt, relationship,
  transition, fallback and station-name modules.
- `home/profile.py` verifies private compatibility files and SQLite bindings.
  `home/bindings.py` provides immutable rules; `home/compatibility.py` resolves
  startup authority and `home/consent.py` stores the permanent narrow scope.

Keep extraction work behavior-preserving. Follow the
[refactor checklist](runbooks/refactor-cuts.md) and explicitly scope changes to
runtime ownership before moving code.
