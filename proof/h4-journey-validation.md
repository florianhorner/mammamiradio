# H4 PR1 — First Listen journey validation

Workspace `kathmandu`. Branch `florianhorner/chore/h4-remaining-work`.
Base at execution: `338ff96c` (#1118 squash). Local `HEAD` remains that
commit. `origin/main` later moved to `c5552122` during this work and was
not re-integrated; do that cleanly before `/ship`.

This is the recovery candidate for **completing the First Listen journey**
(invitation, continuous music, finale handoff, privacy/polling recovery, live
transport, bounded opening cushion). It is not the later radio-world PR2.

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
mammamiradio/web/static/audio/first_listen/welcome.mp3
mammamiradio/web/static/audio/spoken_assets.json
scripts/generate-first-listen-guide.py
tests/web/first_listen_browser_smoke.js
tests/web/test_first_listen_browser_smoke.py
tests/web/test_admin_first_listen.py
tests/web/test_first_listen_narration_assets.py
tests/web/test_streamer_routes.py
tests/web/test_design_tokens.py
docs/integrations/ha-integration.md
docs/runbooks/first-listen-local-ha.md
docs/runbooks/ha-addon.md
proof/h4-journey-validation.md
proof/preship-reviews/v2/**
```

`scripts/validate-spoken-assets.py` stayed on main (`include_admin_opening`).
README and `tests/home/test_ha_media_source.py` stay with #1079.
No household-scene markup, setup Done/Back, or `returnToFirstListenSetup`.
No integrator files (pyproject, changelogs, addon version, workflows).

## Audio identity

| Asset | Blob / digest | Disposition |
|---|---|---|
| `web/static/audio/first_listen/welcome.mp3` | git blob `3cae77f238337f0e53456c5e631ccce51d9b5f2a`; sha256 `dd31f04724119647c9152b9f9e7d7a0e5abf266d2b9d7d4e368fee4e789c126a`; 12.528s | Invitation welcome (“Three small steps”). UI rounds to 13 seconds. |
| `assets/demo/first_listen/first_listen_admin_show.mp3` | `4756c0f698e8ec03e4865dc1dfa929ad674b2c25` | Unchanged English Admin opening from main. |
| `privacy.mp3`, `ai.mp3`, demo `spoken_assets.json` | main | Unchanged. |

Generator welcome lines match the invitation transcript. No paid regeneration.

## Size

`git diff --stat 338ff96c` on the product/test/docs write-set: 16 files,
**2,135 insertions / 629 deletions = 2,764 changed text lines**, plus the
welcome binary. This proof file is extra. Envelope was ~3,200 including tests
and fresh proof (~30% stop). One v2 receipt remains after review.

## Checks that were actually run

| Check | Observed result |
|---|---|
| Spoken-asset validator (`python scripts/validate-spoken-assets.py --browser-assets-root mammamiradio/web/static/audio`) | Valid: manifest, receipt, hashes, transcripts, admin metadata, media format, loudness, routes, bundle size |
| Welcome hash vs working-tree bytes | Match `dd31f047…` |
| Focused First Listen / narration / design-token / new streamer tests, including `requires_ffmpeg` packaged-opening bitrate | **209 passed** |
| Real First Listen browser harness against isolated app `http://127.0.0.1:8765` | **1 passed** after ducking-vs-#1118 and Playwright-host timer fixes; contract test also passed |
| `COVERAGE_RATCHET_XDIST=4 make check` | Exit 0. Lint, format, types, dead-code, and media-check passed (`media-proof: PASS`). Coverage pytest: **9031 passed, 4 skipped**, 93% total against a 92% floor; all per-module floors held. Default `addopts` still deselect `requires_ffmpeg`. The four skips are opt-in/env tests (browser harnesses without `ADMIN_BROWSER_SMOKE_URL`, plus other fixture skips). |
| `scripts/first-listen-lab.sh` / live Home Assistant | **Not run.** Isolated local app only. Lab state stays under `tmp/first-listen-ha-lab/` if used later. |
| Human perceptual timing (duck/restore, cushion feel) | **Unmeasured** on this candidate. Prior listening applies to the reused welcome bytes only. |

`pytest` default `addopts` still deselect `requires_ffmpeg` except where this
validation invoked those tests explicitly. Browser harness is opt-in and was
run with `ADMIN_BROWSER_SMOKE_URL` / `PLAYER_SMOKE_URL` set.

## Browser harness coverage

`tests/web/first_listen_browser_smoke.js` exercised against the isolated app,
including:

- Fresh / degraded source / re-entry / restart
- Private and enabled Home details, delayed/lost/malformed privacy replies
- Receipt recovery, preview expiry, hydration / lost-continuity retain on the privacy step
- Music continues under hosts (duck, not pause); #1118 interrupted guide-load invalidation with station preserved
- `explicit-listener-exit` iframe handoff; one playback owner; live rejoin on listener resume
- Live Pause / Continue / Skip / Ban / force-Continue (`first_listen=live`)
- Toast/Undo clearance above the local player
- Desktop and phone widths plus 200% finale and player geometry
- `explicit-listener-exit` stage name kept (not renamed to `listener-page-handoff`)

#1121 Skip toast wording was left on main (“DJ handoff in progress…”). Slice C
`script_guard` diagnostics stay in admin status.

No Cursor browser tools were available for an extra manual click-through.
The Playwright harness is the end-to-end UI proof for this candidate.

## Integration notes

- Built onto `338ff96c`, not a two-dot replace with `5ed25731`.
- `stopFirstListenGuide` increments `_firstListenGuideAttempt` before pause/reset, then ducks via `setFirstListenMusicGain`.
- `/stream?first_listen=live` uses `prelude=False`. Skip no longer calls `pacer.reset_timeline("explicit_skip")`.
- README may still say **Yes, I hear it** until #1079; Admin says **I hear you**.

A first `make check` failed because the admin.html three-way merge had dropped
later main sticky-deck chrome (`initDeckPinned`, sentinel, transparent rest
fill) and a few grandfathered setup strings. Those were restored from
`338ff96c` without taking Scaletta/Slice D work. First Listen CSS now hides
`.mmr-deck-sentinel` with the deck.

## Still required before `/ship`

1. Conventional commits of this candidate (if not already on the branch).
2. Parallel read-only reviews (privacy/recovery, playback/queue/binaries, tests/UI/docs).
3. `scripts/emit-review-evidence.sh` and the receipt-only commit.
4. Integrate current `origin/main` if it is still ahead.
5. Explicit `/ship`. Do not open the PR before that authorization.
