# Repo Map — Where Things Live

If you want to fix or extend X, look in Y. The folder hierarchy IS the mental model (leadership principle #4).

## Source code

| What you want to change                            | Where to look                                |
|----------------------------------------------------|-----------------------------------------------|
| What hosts say (banter, jokes, callouts)           | `mammamiradio/hosts/scriptwriter.py`         |
| Host personality, listener memory, motifs          | `mammamiradio/hosts/persona.py`              |
| Time-of-day / cultural cues injected into prompts  | `mammamiradio/hosts/context_cues.py`         |
| Ads (brands, voices, campaign spines)              | `mammamiradio/hosts/ad_creative.py`          |
| Music sources (charts, Jamendo, local files)       | `mammamiradio/playlist/playlist.py`          |
| yt-dlp / Jamendo / local file fetch                | `mammamiradio/playlist/downloader.py`        |
| Per-track rules ("skip the bridge", anthems)       | `mammamiradio/playlist/track_rules.py`       |
| Per-track machine-derived song memory              | `mammamiradio/playlist/song_cues.py`         |
| "Why this track?" rationale generation             | `mammamiradio/playlist/track_rationale.py`   |
| FFmpeg normalize / mix / concat / SFX              | `mammamiradio/audio/normalizer.py`           |
| Edge TTS / OpenAI TTS synthesis                    | `mammamiradio/audio/tts.py`                  |
| Audio quality gate (duration, silence checks)      | `mammamiradio/audio/audio_quality.py`        |
| Voice catalog (Edge voice IDs)                     | `mammamiradio/audio/voice_catalog.py`        |
| Home Assistant polling / state formatting          | `mammamiradio/home/ha_context.py`            |
| HA event derivation (diffs, pruning)               | `mammamiradio/home/ha_enrichment.py`         |
| Segment scheduling (banter / ad / music)           | `mammamiradio/scheduling/scheduler.py`       |
| Producer loop (queue ahead of playback)            | `mammamiradio/scheduling/producer.py`        |
| WTF clip extraction + ring buffer                  | `mammamiradio/scheduling/clip.py`            |
| HTTP routes / playback loop / auth                 | `mammamiradio/web/streamer.py`               |
| Open Graph share card                              | `mammamiradio/web/og_card.py`                |
| Listener / admin / live HTML                       | `mammamiradio/web/templates/`                |
| CSS / JS / icons / service worker                  | `mammamiradio/web/static/`                   |
| `radio.toml` parsing + `.env`                      | `mammamiradio/core/config.py`                |
| Shared data models (Track, Segment, etc.)          | `mammamiradio/core/models.py`                |
| Capability flags + tier derivation                 | `mammamiradio/core/capabilities.py`          |
| Legacy setup-status classification                 | `mammamiradio/core/setup_status.py`          |
| SQLite schema / migrations                         | `mammamiradio/core/sync.py`                  |
| App startup / shutdown lifecycle                   | `mammamiradio/main.py`                       |
| Demo MP3s / SFX / studio bleeds / logo             | `mammamiradio/assets/`                       |

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
| Design system (colors, fonts)    | `docs/design/system.md`          |
| Admin panel layout standards     | `docs/design/admin-panel.md`     |
| Conductor workspace lifecycle    | `docs/conductor.md`              |
| Cathedral restructure plan       | `docs/2026-04-28-cathedral-restructure.md` |

## God modules pending split

Two modules carry a `# TODO: split` marker referencing the cathedral plan:

- `mammamiradio/web/streamer.py` (~2,400 LOC) — splits in PR 5 of the cathedral plan into `routes_listener.py`, `routes_admin.py`, `auth.py`, `playback_loop.py`, `public_status.py`
- `mammamiradio/hosts/scriptwriter.py` (~1,500 LOC) — splits in PR 6 into `banter.py`, `ads.py`, `llm_client.py`, `fallbacks.py`

Until those PRs land, these modules are postal addresses, not destinations. Ride the structure that exists today; do not pre-split.
