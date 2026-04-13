# 2026-04-13 Log Resolution Plan

## Purpose

Turn the 2026-04-13 runtime log review into a resolution plan that can be split across parallel agents.

This document is intentionally written as an execution plan, not a postmortem. It names the failed user-visible promises, the most likely code seams, the automated guards required for each fix, and the dependency graph between workstreams.

## Source Material

- Primary input: local workspace log captured on 2026-04-13
- Key recurring themes extracted from that log:
  - lifecycle churn and repeated restarts
  - queue starvation while listeners are connected
  - Anthropic auth failures with OpenAI fallback
  - TTS voice mismatch fallback
  - ffmpeg normalization failures leading to silence insertion
  - playlist/content hygiene drift

## Planning Rules

- Do not treat the first broken line as the only broken path.
- Every behavior change needs an automated guard in the same workstream.
- Prefer minimal, isolated diffs per workstream so multiple agents can merge without stepping on each other.
- Preserve the "station keeps broadcasting" invariant even while tightening failure handling.

## Success Criteria

- A fresh listener should not see repeated `Queue empty for 30s` warnings in normal startup conditions.
- Invalid provider credentials or voice config should degrade once, cleanly, without repeated avoidable errors.
- A bad track or killed ffmpeg job should skip or quarantine the asset rather than collapsing into silence unless all fallback paths are exhausted.
- Music mode should stop admitting obvious non-music or too-short assets.
- Startup and shutdown causes should be reconstructable from logs and status without grep archaeology.
- Each resolved invariant should have a regression test or equivalent automated check.

## Seams And Ownership Boundaries

These are the preferred ownership seams for swarm execution.

| Workstream | Primary seam | Likely files | Primary tests |
| --- | --- | --- | --- |
| WS1 | lifecycle + observability | `mammamiradio/main.py`, `mammamiradio/streamer.py`, `start.sh`, `ha-addon/mammamiradio/rootfs/run.sh`, `conductor.json` | `tests/test_main.py`, `tests/test_start_sh.py`, `tests/test_repo_scripts.py` |
| WS2 | queue fill + readiness | `mammamiradio/producer.py`, `mammamiradio/streamer.py`, `mammamiradio/scheduler.py` | `tests/test_shadow_queue_sync.py`, `tests/test_producer_extended.py`, `tests/test_streamer_routes.py`, `tests/test_streamer.py` |
| WS3 | provider fallback correctness | `mammamiradio/scriptwriter.py`, `mammamiradio/tts.py`, `mammamiradio/config.py`, `mammamiradio/capabilities.py` | `tests/test_scriptwriter.py`, `tests/test_tts.py`, `tests/test_config.py`, `tests/test_capabilities.py` |
| WS4 | audio render resilience | `mammamiradio/normalizer.py`, `mammamiradio/producer.py`, `mammamiradio/downloader.py`, `mammamiradio/audio_quality.py` | `tests/test_normalizer_unit.py`, `tests/test_normalizer_extended.py`, `tests/test_audio_quality.py`, `tests/test_producer_unit.py` |
| WS5 | source and content hygiene | `mammamiradio/playlist.py`, `mammamiradio/downloader.py`, `mammamiradio/audio_quality.py` | `tests/test_playlist.py`, `tests/test_playlist_fetch.py`, `tests/test_downloader.py`, `tests/test_audio_quality.py` |
| WS6 | soak verification + docs | `TROUBLESHOOTING.md`, `OPERATIONS.md`, `HA_ADDON_RUNBOOK.md`, `README.md` if needed | targeted smoke checks, existing repo tests, optional add-on validation scripts |

## Workstreams

### WS1: Lifecycle And Observability Hardening

**Failed promise**

Operators should be able to explain why the station restarted or stopped from a small set of structured lines.

**Evidence windows**

- `20:48:16-01:25:04`
- `15:58:45-15:59:20`
- `16:12:47-18:13:20`
- `18:44:36`

**Symptoms seen**

- repeated `Starting add-on...`
- repeated `Shutting down` and `Finished server process`
- stopped-session restoration on startup
- prewarm timeout after startup
- explicit admin stop events mixed into other restart noise

**Likely root-cause buckets**

- normal admin stop and resume state is hard to distinguish from external process restarts
- startup summary is not yet sufficient to explain degraded provider paths or readiness state
- restart reason is spread across app logs, uvicorn logs, and s6 logs

**Resolution goals**

- emit one structured startup summary line with enough fields to explain mode and degradations
- emit one structured shutdown summary line with explicit reason when the app knows it
- make admin stop/resume state visible in `/status` and relevant public/admin status payloads if missing or ambiguous
- ensure add-on lifecycle scripts and Conductor hooks still match the runtime contract if startup semantics change

**Automated guards**

- add or extend tests for startup summary contents
- add or extend tests for shutdown or stop-reason semantics where possible
- add repo-script coverage if lifecycle hooks or startup shell behavior changes

**Failure containment**

- keep this workstream mostly to logging, status serialization, and lifecycle shell entrypoints
- avoid touching queue scheduling logic here

### WS2: Queue Fill And Listener Starvation

**Failed promise**

A listener connecting to a healthy station should hear live audio promptly, not repeated queue-empty waits while the system claims to be up.

**Evidence windows**

- `15:41:57-15:47:47`
- `15:59:23-16:00:57`
- `18:14:34-18:18:49`
- `18:39:15-18:40:24`

**Symptoms seen**

- `Queue empty for 30s, waiting...` repeated 24 times
- listener connect/disconnect churn around empty queue windows
- `Prewarm timed out after 20s`
- eventual recovery once the first segment lands

**Likely root-cause buckets**

- readiness and startup-complete semantics are ahead of actual stream readiness
- the producer is not filling quickly enough under certain startup or listener-arrival paths
- stopped-session restore and queue prewarm may interact badly

**Resolution goals**

- reproduce the empty-queue windows in a deterministic test path if possible
- align `readyz`, startup status, and first-listener experience with actual queue state
- reduce or eliminate the "30s wait loop" in nominal startup
- verify that admin stop/resume and restart flows do not strand listeners in an empty queue

**Automated guards**

- add coverage around first-listener startup behavior
- add coverage around queue refill after resume or restart
- add or extend shadow queue synchronization tests if queue state is duplicated anywhere

**Failure containment**

- keep changes centered in `producer.py`, `streamer.py`, and readiness semantics
- do not fold provider or playlist filtering changes into this workstream

### WS3: Provider Fallback Correctness

**Failed promise**

Bad optional provider configuration should degrade once and cleanly, not spam repeated avoidable failures on every generation path.

**Evidence windows**

- Anthropic auth failures across `15:47:48-15:55:57`, `16:03:07-16:11:09`, `18:20:53-18:42:03`
- TTS voice mismatch at `15:56:44`, `16:09:53`, `18:30:33`, `18:40:54`

**Symptoms seen**

- repeated `401 Unauthorized` / `invalid x-api-key`
- repeated `Anthropic AuthenticationError, falling back to OpenAI`
- repeated `Invalid voice 'onyx'`
- successful fallback after each failure

**Likely root-cause buckets**

- Anthropic failures are not being memoized or downgraded for the remainder of the session
- voice validation happens too late, after a request is already attempted
- capability/status surfaces may not reflect degraded provider state clearly enough

**Resolution goals**

- avoid repeated Anthropic auth attempts once the key is known-bad for the current process or cooldown window
- validate or normalize TTS voice configuration before synthesis attempts
- expose degraded provider path clearly in logs and status
- check sibling code paths so ads, banter, and transitions share the same fallback correctness

**Automated guards**

- tests showing auth failure causes one downgrade decision, not a flood of retries
- tests for invalid OpenAI voice mapping or fallback selection
- tests for capability/status reporting when one provider is degraded

**Failure containment**

- keep this workstream in provider selection, config validation, and fallback policy
- do not change queue timing or ffmpeg behavior here

### WS4: Audio Render Resilience

**Failed promise**

One bad render should not force dead air if another safe playback path still exists.

**Evidence windows**

- `15:58:45`
- `18:44:36`

**Symptoms seen**

- ffmpeg normalization finishes almost entirely, then exits with status `255`
- producer logs `Failed to produce music segment`
- station inserts silence because no canned clips are available

**Likely root-cause buckets**

- ffmpeg is being terminated externally or the process wrapper is treating termination as a hard failure without a safe retry path
- failed music assets are not being quarantined
- silence insertion is being chosen too early, before alternative tracks or safe placeholders are exhausted

**Resolution goals**

- distinguish external termination from real media/command failure where practical
- quarantine or mark bad assets to avoid repeated attempts
- on music render failure, skip to the next viable track before falling back to silence
- verify sibling normalization paths for banter and ads so the same failure mode is not waiting elsewhere

**Automated guards**

- tests for music render failure recovery
- tests that bad assets are skipped or quarantined
- tests for fallback ordering before silence insertion

**Failure containment**

- keep scope limited to render pipeline error handling and fallback ordering
- avoid mixing metadata filtering decisions from WS5 into this branch unless absolutely required

### WS5: Source And Content Hygiene

**Failed promise**

Music mode should play music-like assets of sane duration, not obvious junk, clips, or malformed cache entries.

**Evidence windows**

- short-track rejections at `18:16:56` and `18:32:23`
- suspicious content admission around `18:37:03-18:38:37`

**Symptoms seen**

- `music audio too short (2.42s < 30.00s)`
- `music audio too short (6.12s < 30.00s)`
- obviously odd selections such as `BBC Studios – Do You Speak English? - Big Train - BBC comedy`
- multiple cache hits for questionable titles

**Likely root-cause buckets**

- ingestion accepts titles or sources that should be rejected before queueing
- duration and media-shape validation happens too late
- rejected assets can still remain attractive cache hits

**Resolution goals**

- tighten title/source heuristics for chart/downloaded content
- validate duration and media shape before an asset becomes a normal queue candidate where practical
- quarantine or blacklist rejected assets within the session
- check sibling ingestion paths: charts, local files, manual playlist additions, and restored source state

**Automated guards**

- tests for rejecting obviously non-music titles or too-short assets
- tests ensuring rejected content is not immediately re-selected from cache
- tests that valid tracks still pass so the filter does not become over-aggressive

**Failure containment**

- keep this workstream focused on ingestion, metadata, and quality-gate entry policy
- do not change provider fallback or queue scheduling here

### WS6: Soak Verification And Operator Docs

**Failed promise**

After fixes land, operators should know how to verify them and how the new fallback behavior is supposed to look in logs.

**Depends on**

- WS1 through WS5 landing first or stabilizing enough for testable behavior

**Resolution goals**

- run a 30-60 minute local or add-on shaped soak with listeners connecting and disconnecting
- verify no repeated queue-empty warnings in nominal startup
- verify provider degradation logs are intentional and not spammy
- verify bad tracks are skipped cleanly
- document the new expected failure and fallback signatures

**Docs likely to update**

- `TROUBLESHOOTING.md`
- `OPERATIONS.md`
- `HA_ADDON_RUNBOOK.md`
- `README.md` if user-visible behavior or setup expectations change

**Automated guards**

- keep code-level tests in the earlier workstreams
- use this workstream for smoke scripts, manual verification matrices, and doc sync

**Failure containment**

- no opportunistic product changes
- no broad refactors masked as documentation cleanup

## Dependency Graph

The workstreams are intentionally mostly independent.

- WS1 can start immediately.
- WS2 can start immediately.
- WS3 can start immediately.
- WS4 can start immediately.
- WS5 can start immediately.
- WS6 should start after the others have at least draft PRs or a stable branch to test.

The only likely conflict pair is WS4 and WS5 because both may touch `downloader.py` or `audio_quality.py`. If both are active at once, keep ownership explicit:

- WS4 owns render-time failure handling and fallback ordering.
- WS5 owns ingest-time filtering and candidate selection.

## Suggested Swarm Splits

### Option A: 3-agent split

- Agent 1: WS1 + WS6
- Agent 2: WS2
- Agent 3: WS3 + WS4 + WS5

Use this if the swarm is small and merge bandwidth matters more than isolation.

### Option B: 5-agent split

- Agent 1: WS1
- Agent 2: WS2
- Agent 3: WS3
- Agent 4: WS4
- Agent 5: WS5, then WS6 once the others are ready

This is the cleanest default split. It follows the code seams with minimal overlap.

### Option C: 6-agent split

- Agent 1: WS1
- Agent 2: WS2
- Agent 3: WS3 Anthropic auth degradation
- Agent 4: WS3 TTS voice validation
- Agent 5: WS4
- Agent 6: WS5, then WS6 verification/docs

Use this only if the swarm is comfortable reconciling two PRs inside the provider seam.

## Per-PR Definition Of Done

Every PR spawned from this plan should include:

1. the invariant it restores
2. the sibling paths checked for the same failure mode
3. at least one automated guard that would fail before the fix
4. any required doc updates in the same PR if behavior changed
5. a short note on residual risk

## Recommended Execution Order

If there is no reason to prefer another order:

1. Start WS2, WS3, WS4, and WS5 in parallel.
2. Run WS1 in parallel if the agent is disciplined about staying out of queue logic.
3. Hold WS6 until the behavior-level fixes have stabilized.

This is a recommendation, not a hard requirement. If the swarm has stronger ownership around provider or audio pipeline code, reorganize accordingly.

## Handoff Prompt Template

Use this to launch a swarm task from the plan:

```text
Take WSX from docs/2026-04-13-log-resolution-plan.md.

Goal: restore the named invariant with a minimal diff.
Constraints:
- Python FastAPI project
- add automated guards
- do not stop at the first broken path; check sibling paths
- if lifecycle hooks change, update conductor.json and related scripts
- keep docs in sync for behavior/config/auth/fallback changes

Deliver:
- code
- tests
- short merge note: invariant restored, sibling paths checked, residual risk
```
