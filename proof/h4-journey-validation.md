# H4 — complete First Listen recovery validation

Workspace `kathmandu`. Branch `florianhorner/chore/h4-remaining-work`.
Base at first execution: `338ff96c` (#1118 squash). Three-step PR1 cut
`ea9fb00c` + copy fix `1397db47`. Integrated current `origin/main`
(`bb6c3aeb` #1123 and `c5552122` #1125) as merge `5b0b5040`.
`origin/main` is an ancestor of HEAD.

This is the recovery candidate for the **complete polished First Listen**
already accepted on the stack tip: invitation, both-host welcome, household
scenes, setup Done/Back, free-voice audition, later privacy/AI recordings,
recorded show after song one, continuous music, finale, privacy/polling
recovery, live transport, and the bounded opening cushion. Main’s English
Admin opening (`4756c0f6`) stays. It is no longer a PR1-only cut.

Historical listening of the accepted invitation welcome stands for those
unchanged bytes. It does not replace checks on this assembled candidate.

The v2 receipt under `proof/preship-reviews/v2/` and `git log origin/main..HEAD`
are the exact-head record after review. This file is the run narrative.

## Write-set

```text
mammamiradio/web/templates/admin.html
mammamiradio/web/static/first-listen.css
mammamiradio/web/static/listener.js
mammamiradio/web/streamer.py
mammamiradio/main.py
mammamiradio/scheduling/producer.py
mammamiradio/web/static/audio/first_listen/welcome.mp3
mammamiradio/web/static/audio/first_listen/privacy.mp3
mammamiradio/web/static/audio/first_listen/ai.mp3
mammamiradio/web/static/audio/spoken_assets.json
mammamiradio/web/static/audio/voice_examples/free-voices.mp3
mammamiradio/web/static/audio/voice_examples/spoken_assets.json
scripts/generate-first-listen-guide.py
scripts/validate-spoken-assets.py
tests/web/first_listen_browser_smoke.js
tests/web/test_first_listen_browser_smoke.py
tests/web/test_admin_first_listen.py
tests/web/test_first_listen_narration_assets.py
tests/web/test_streamer_routes.py
tests/web/test_design_tokens.py
tests/web/test_admin_regia_polish.py
tests/web/test_admin_status_invariants.py
tests/web/test_main.py
tests/scheduling/test_queue_commit_contract.py
tests/repo/test_repo_scripts.py
docs/integrations/ha-integration.md
docs/runbooks/first-listen-local-ha.md
docs/runbooks/ha-addon.md
proof/h4-journey-validation.md
proof/preship-reviews/v2/**
```

README and `tests/home/test_ha_media_source.py` stay with #1079.
`#1123` model-registry tests stay. No integrator files (pyproject,
changelogs, addon version, workflows). Tip opening `20951268` was not taken.

## Audio identity

| Asset | Blob / digest | Disposition |
|---|---|---|
| `web/static/audio/first_listen/welcome.mp3` | git blob `370ed0cc8710cbef90e68f3e18fc76fdd69c515c`; sha256 `de7e0d29bed865e35710d0929dcc1825cc2b32eba4366099ca66554b65ecc97c`; 15.768s | Both-host welcome (“I run the desk”). UI rounds to 16 seconds. |
| `web/static/audio/first_listen/privacy.mp3` | git blob `cab7a2b29ad0723392d8bc5122974f8a8a1436bc`; sha256 `17bde6abcd1a431773b55e4a2fd32bdd203a7121d00dedaa28ded05556d5b426`; 9.504s | Rooftop-disco Home recording. |
| `web/static/audio/first_listen/ai.mp3` | git blob `345a7f1ace5001186e4c09fa9021d36bfc624d17`; sha256 `5e30ee5ce2a1f20564e53182b0828caaf895d2a73ea1d6fef1c47d3d9dd50c1a`; 9.312s | Writing-service / cousin recording. |
| `web/static/audio/voice_examples/free-voices.mp3` | git blob `1bf009e40c07477da4066f7602a3f2c662764bc3`; sha256 `eff2f05076122dd021d0f6cba9d73f21f17983885bf35c8dcaf33d38b12f494e`; 12.192s | Free-voice audition. |
| `mammamiradio/assets/demo/first_listen/first_listen_admin_show.mp3` | `4756c0f698e8ec03e4865dc1dfa929ad674b2c25` | Unchanged English Admin opening from main. Tip opening `20951268` was not taken. |

Generator welcome/privacy/AI/free-voice lines match those transcripts. Opening
clip text stays on main (`Mamma Mi Radio, live from Studio B`). No paid
regeneration.

## Size

`git diff --stat origin/main` on this assembled candidate: **29 files,
3,484 insertions / 802 deletions**, plus the four audio binaries (welcome,
privacy, ai, free-voices). Florian asked for the complete polished stack on
one branch; the old PR1-only ~3,200 envelope does not apply. Stop before
~30% growth of this declared combined write-set.

## Checks that were actually run

| Check | Observed result |
|---|---|
| Spoken-asset validator (`python scripts/validate-spoken-assets.py --browser-assets-root mammamiradio/web/static/audio`) | Valid: manifest, receipt, hashes, transcripts, admin metadata, media format, loudness, routes, bundle size |
| UI copy lint (`bash scripts/check-ui-copy-lint.sh`) | Clean (6 known violations baselined) |
| Ruff on touched Python | Clean after import sort + two wrapped E501 asserts |
| Focused First Listen / Admin / narration / design-token / streamer / startup / queue tests (`-m 'not requires_ffmpeg'`) | **322 passed, 6 deselected** |
| Real First Listen browser harness against isolated app `http://127.0.0.1:8765` | **1 passed** in 42.66s. Contract test also passed. Household scenes, Make it yours, free-voices `voice_examples` source, Done/Back, and the prior PR1 walk all ran. |
| `COVERAGE_RATCHET_XDIST=4 make check` | Lint, format, types, dead-code, and media-check passed (`media-proof: PASS`). First combined `make check` died printing coverage stdout (`BrokenPipeError`, make exit 120). Clean `scripts/coverage-ratchet.py check` afterward: **9192 passed, 4 skipped**, 93% total against a 92% floor; all per-module floors held. Default `addopts` still deselect `requires_ffmpeg`. The four skips are opt-in/env tests (browser harnesses without `ADMIN_BROWSER_SMOKE_URL`, plus other fixture skips). |
| `scripts/first-listen-lab.sh` / live Home Assistant | **Not run.** Isolated local app only. Lab state stays under `tmp/first-listen-ha-lab/` if used later. |
| Human perceptual timing (duck/restore, cushion feel) | **Unmeasured** on this candidate. Prior listening applies to reused bytes only. |

`pytest` default `addopts` still deselect `requires_ffmpeg` except where this
validation invoked those tests explicitly. Browser harness is opt-in via
`ADMIN_BROWSER_SMOKE_URL`.

## Browser harness coverage

`tests/web/first_listen_browser_smoke.js` includes the prior PR1 walk plus:

- `guided-connection`: three household scenes, **Make it yours**, free-voices
  play through `voice_examples`, Home choice, Done/Back buttons present
- Fresh / degraded source / re-entry / restart
- Private and enabled Home details, delayed/lost/malformed privacy replies
- Receipt recovery, preview expiry, hydration / lost-continuity retain
- Music continues under hosts (duck, not pause); #1118 interrupted guide-load
  invalidation with station preserved
- `explicit-listener-exit` iframe handoff (stage name kept; report list may
  still say `listener-page-handoff`); one playback owner; live rejoin
- Live Pause / Continue / Skip / Ban / force-Continue (`first_listen=live`)
- Toast/Undo clearance above the local player
- Desktop and phone widths plus 200% finale and player geometry

#1121 Skip toast wording was left on main (“DJ handoff in progress…”). Slice C
`script_guard` diagnostics stay in admin status.

No Cursor browser tools were available for an extra manual click-through.
The Playwright harness is the end-to-end UI proof once it is re-run.

## Integration notes

- Built onto current main, not a two-dot replace with `5ed25731`.
- `stopFirstListenGuide` increments `_firstListenGuideAttempt` before pause/reset, then ducks via `setFirstListenMusicGain`.
- `/stream?first_listen=live` uses `prelude=False`. Skip no longer calls `pacer.reset_timeline("explicit_skip")`.
- Cold install orders music → third-chair banter → music, then ordinary production after a 15s opening wait.
- README may still say **Yes, I hear it** until #1079; Admin says **I hear you**.

Read-only reviews after `ea9fb00c`: no P0. Playback/transport clean. Privacy
recovery clean on fail-closed behavior (three P2 copy/display notes). Tests/docs
had one P1 stale “Check the sound” recovery action — fixed to **Can you hear us?**
and guarded in `test_admin_first_listen.py`.

## Still required before `/ship`

1. `scripts/emit-review-evidence.sh` after the official gstack review
   ledger covers HEAD. Informal Cursor reviews are not a ledger; do not invent one.
2. Explicit `/ship`. Do not open the PR before that authorization.
