# Producer Refactor Plan

## Context

`mammamiradio/producer.py` currently mixes four different responsibilities inside `run_producer()`:

1. runtime policy
2. segment building
3. shared state mutation
4. failure recovery

That shape is now the main fragility point in the app. The immediate operational pressure is Pi queue starvation and dead air, but the underlying design pressure is that producer behavior is coupled to `StationState`, `streamer.py`, and several UI contracts.

This plan is intentionally not a flag-day rewrite. The target is a new producer core behind the existing `run_producer()` boundary so the live contracts stay stable while the internals change.

## Non-Negotiable Invariants

These are the contracts that must stay true through the refactor:

- Source switches must invalidate in-flight work via `playlist_revision`; stale segments must never leak into the new playlist context.
- `session_stopped` must discard completed in-flight builds without queueing them or advancing playback counters.
- Listener requests must only be consumed when generated banter queues successfully. Canned fallback and impossible-TTS fallback must not consume them.
- The real queue and `state.queued_segments` shadow must remain synchronized by explicit rules. The producer appends shadow entries only after successful queue commit. The streamer removes them when playback actually starts.
- Failures must degrade to canned audio or silence instead of crashing the station.
- Music production, prewarm, and future watermark logic must share one build path. No second copy of the music pipeline.

## Decision

Build a new producer core behind the existing `run_producer()` facade.

Do not:

- rewrite `StationState` first
- replace `run_producer()` in one jump
- combine extraction with multi-producer concurrency
- remove the queue shadow in the same effort

Do:

- introduce internal modules with clear ownership
- make state mutation pass through one commit boundary
- extract one vertical slice at a time, starting with music

## Target Structure

Proposed internal package:

- `mammamiradio/production/types.py`
- `mammamiradio/production/commit.py`
- `mammamiradio/production/policy.py`
- `mammamiradio/production/music.py`
- `mammamiradio/production/banter.py`
- `mammamiradio/production/ads.py`
- `mammamiradio/production/misc.py`

The public import boundary stays where it is:

- `mammamiradio/producer.py`

That file becomes a shell that:

- chooses the next segment type
- calls the appropriate builder
- commits or rejects the build result
- handles recovery and logging

## Core Seams

### 1. Build Result

Introduce one internal result type so builders stop directly mutating shared state:

- `segment`
- `shadow_entry`
- `on_commit`
- `on_reject`
- `telemetry`

The important part is not the exact type name. The important part is that builders return intent and the shell performs the mutation.

### 2. Commit Boundary

Add one commit function that alone is allowed to:

- check `playlist_revision`
- check `session_stopped`
- queue the segment
- append to `state.queued_segments`
- run success callbacks
- reset or retain failure counters

This is the synchronization boundary for the producer side.

### 3. Policy Boundary

Move these decisions out of segment builders:

- idle and resume behavior
- lookahead thresholds
- chart refresh cadence
- cache eviction cadence
- queue-watermark urgency
- fallback policy when the queue is dry

## Delivery Plan

## PR1: Characterize and Create the Commit Boundary

Goal: stabilize the contracts before moving logic.

Changes:

- add or tighten characterization tests around:
  - source switch invalidation
  - stopped-session discard
  - listener-request commit behavior
  - queue-shadow behavior
- add `BuildResult` and `commit_built_segment(...)`
- keep `run_producer()` behavior-identical

Exit condition:

- `run_producer()` still owns the flow, but all successful queue commits go through one path

## PR2: Extract Music and Unify Prewarm

Goal: remove the most duplicated and performance-sensitive path first.

Changes:

- create `production/music.py`
- move music build logic there:
  - track selection
  - download validation
  - normalization cache
  - quality gate
  - rationale generation
  - studio bleed
- make `prewarm_first_segment()` call the same music builder in prewarm mode

Exit condition:

- there is only one music build path in the codebase

## PR3: Extract Runtime Policy and Land Watermarks

Goal: make Pi recovery work a policy change, not another layer inside the music builder.

Changes:

- create `production/policy.py`
- move:
  - idle and resume behavior
  - queue fullness checks
  - chart refresh timing
  - cache eviction timing
- implement queue-watermark urgency from the current backlog

Not in this phase:

- no parallel ffmpeg jobs yet
- no multi-producer design

Exit condition:

- the producer shell reads like an explicit runtime loop rather than a monolith

## PR4: Extract Banter

Goal: separate the most behavior-sensitive non-music path.

Changes:

- create `production/banter.py`
- move:
  - impossible moment logic
  - canned fallback selection
  - transition generation
  - dialogue synthesis
  - banter quality fallback
- move listener-request consumption behind `on_commit`

Exit condition:

- banter builders no longer directly consume pending requests or mutate queue-facing state after build

## PR5: Extract Ads and Misc Segments

Goal: finish the decomposition without changing the public producer boundary.

Changes:

- create `production/ads.py`
- create `production/misc.py`
- move:
  - ad break assembly
  - ad history commit intent
  - station ID
  - sweeper
  - time check
  - news flash

Exit condition:

- `producer.py` is a thin orchestrator

## Decision Gate After PR3

Re-evaluate after policy and music are extracted.

Keep the staged plan if:

- builders return mostly pure results
- commit logic is centralized
- shell complexity is dropping fast

Escalate to a deeper rewrite if:

- builders still need broad direct `StationState` mutation
- commit hooks are leaking special cases everywhere
- queue policy and build policy still cannot be separated cleanly

## What To Challenge In Review

- Is the commit boundary too wide or too narrow?
- Should ad history be recorded during build or only on commit?
- Should `last_banter_script` and `last_ad_script` become commit outputs rather than direct state writes?
- Is queue-watermark logic safe before introducing concurrent normalize jobs?
- Should `state.queued_segments` stay a producer responsibility, or should that move to a dedicated queue adapter later?
- Is `prewarm_first_segment()` really a mode of music build, or does it need its own semantics?

## Explicit Non-Goals For The First Pass

- no public API changes
- no admin UI changes
- no `StationState` redesign
- no queue shadow removal
- no switch to a class-heavy framework just for refactor aesthetics
- no parallel build workers in the same pass

## Validation

Minimum suite per phase:

- `tests/test_producer_unit.py`
- `tests/test_producer_extended.py`
- `tests/test_shadow_queue_sync.py`
- `tests/test_ui_control_contracts.py`

New test files should follow the extraction:

- `tests/test_production_music.py`
- `tests/test_production_policy.py`
- `tests/test_production_banter.py`
- `tests/test_production_ads.py`

## Success Criteria

The refactor is succeeding if:

- `producer.py` becomes smaller because responsibility moves out, not because behavior is hidden in harder-to-follow helpers
- new Pi queue-work lands in policy modules instead of inflating the music builder
- the existing runtime invariants remain green without special-case patching
- future features stop defaulting to "add one more branch inside `run_producer()`"
