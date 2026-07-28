# Admitted-Audio Queue Refactor Specification

- **Status:** READY FOR IMPLEMENTATION
- **Decision date:** 2026-07-27
- **Reviewed snapshot:** `776d8cf7`
- **Current `origin/main` at review:** `bee86696` (Edge metadata only; runtime source unchanged)
- **Scope owner:** internal scheduling architecture
- **Lifecycle:** move unchanged to `docs/archive/` when completed or deliberately deferred

## Summary

Mammami Radio will preserve its current listener, operator, HTTP, and Home
Assistant behavior while moving admitted-audio queue ownership behind one deep
internal module.

The destination is an `AdmittedAudioQueue` that uses composition around a
private bounded `asyncio.Queue`. It owns the real queue, its operator-facing
Scaletta projection, task accounting, stable queue identity, pre-air discard
settlement, tail adjacency, continuity placement, and the final stale-admission
gate.

This is not the Live Queue reorder feature. Drag-reorder, play-soon, and issue
`#410` remain separate after this refactor has shipped and soaked.

## Problem Statement

One admitted segment currently requires synchronized changes across startup,
producer, playback, and live-control code. Those callers directly mutate or
inspect:

- the bounded playback queue;
- its private `_queue` storage;
- `StationState.queued_segments`;
- unfinished-task accounting;
- queue-entry IDs;
- tail-adjacency state;
- the capacity-exempt continuity slot and epoch;
- discard telemetry, receipts, reservations, and ephemeral files.

The real queue and Scaletta must describe the same future broadcast order, but
there is no single owner enforcing that invariant. Several drain/filter/rebuild
implementations exist, private producer helpers are imported by playback and
startup, and status code attempts to repair drift using counts rather than
identity.

This lack of locality has already produced queue-shadow drift, repeated rescue
bugs, incomplete cleanup, stale-admission races, and an abstraction gap that
blocks safe Live Queue reorder.

## Goal and Success Criteria

The refactor is complete when:

- one module is the only production owner of the real Segment queue;
- every real queued segment has exactly one stable queue ID and one Scaletta row
  in the same order;
- the continuity slot is intentionally excluded from real-queue depth and
  Scaletta;
- every dequeued item is completed exactly once;
- every removed item receives best-effort discard settlement and safe cleanup;
- all compound queue mutations remain synchronous and event-loop-confined;
- capacity-blocked admission is revalidated before becoming visible;
- startup, producer, playback, controls, and status use the queue interface
  instead of raw queue storage;
- a static repository guard rejects new raw queue, shadow, and slot writes
  outside the owner;
- existing public payloads, listener behavior, operator behavior, and timing
  remain unchanged.

## Locked Decisions

### Architecture

- Build `AdmittedAudioQueue` by composition around a private
  `asyncio.Queue[Segment]`.
- Do not subclass `asyncio.Queue`. Its inherited mutation interface and private
  storage would remain bypass paths.
- Do not use a stateless collection of `(queue, state)` helpers as the final
  design. That would leave admission, playback completion, reads, and
  continuity ownership distributed.
- Keep the existing bounded queue implementation. Do not replace it with a
  custom deque/condition transport.
- Keep `StationState.queued_segments` as the compatibility read model for
  existing serializers, but allow writes only from the queue owner.
- Keep all queue mutation on the asyncio event-loop thread. No lock is added:
  the linearization point is the existing no-`await` critical section.

### Behavior

- Preserve current queue capacity, playback order, fallback order, public
  payloads, and operator controls.
- Preserve the currently airing segment during queued-audio mutations.
- Preserve the three existing admission fences: before egress, after egress,
  and after a capacity wait.
- Preserve queue-tail speech-bed behavior and fail closed when a mutation makes
  the clean predecessor uncertain.
- Preserve protected continuity and earlier air-next promises ahead of ordinary
  far-future audio.
- Keep Home-fact reservation timing as implemented today: reserve before queue
  admission and release on later rejection. Its documentation disagreement is
  recorded but not resolved in this behavior-preserving train.

### Ownership

The admitted-audio module owns:

- the private real queue and all access to its storage;
- queue ID creation and preservation;
- Scaletta row construction, order, consumption, and identity-based repair;
- queue drain/rebuild and unfinished-task accounting;
- append, startup admission, front insertion, removal, purge, and replacement
  mechanics;
- exact-object retraction after a failed post-capacity validity check;
- pre-air discard settlement and best-effort ephemeral cleanup;
- queue-tail adjacency reconciliation;
- continuity-slot placement and queue-related epoch mutation;
- immutable read snapshots for status, health, cache protection, and tests;
- acquisition leases that guarantee exactly-once queue completion.

The admitted-audio module does not own:

- FFmpeg, egress, rendering, or audio-quality policy;
- playlist, source, chaos, blocklist, or Home-fact eligibility decisions;
- rescue candidate selection or continuity ladder order;
- HTTP parsing, authentication, response copy, or UI state;
- `now_streaming`, listener delivery, first-heard evidence, or post-air results;
- domain rules inside Home facts, listener sessions, release campaigns, or
  Moment Receipts.

Callers retain those decisions and provide synchronous validity or settlement
inputs at the queue seam.

## Internal Interface

The queue interface exposes behavior-level operations, not its transport:

- async tail admission with a final synchronous validity check after any
  capacity wait;
- synchronous startup admission;
- synchronous air-next insertion;
- identity-based removal and predicate removal;
- full purge and prepared continuity-plan application;
- explicit queue invalidation for controls such as Stop, including an empty
  queue with blocked producers;
- acquisition of the next real queued segment through a lease;
- claim of the capacity-exempt continuity slot only when the real queue is
  empty;
- immutable snapshots containing order, depth, head, tail, buffered duration,
  protected paths, and Scaletta projection;
- an idle/join operation for shutdown and tests.

The acquisition lease removes the matching Scaletta row when acquired and
settles unfinished work exactly once on every normal, rejected, skipped, or
error exit.

No raw `put`, `get`, `task_done`, `_queue`, or mutable shadow interface is
exposed to production callers.

## Queue Invariants

### Identity and Projection

- Queue identity is stable from pre-egress projection through terminal queue
  transition.
- Real queue membership and Scaletta membership are one-to-one and ordered.
- Drift repair rebuilds from stable real-queue IDs, never from depth alone.
- The continuity slot has no Scaletta row and does not affect queue depth.

### Admission

- Rejected work publishes neither a real queue item nor a Scaletta row.
- A capacity wait may yield, but publication after the wait is synchronous.
- A stale item is retracted by object identity before callbacks, projection,
  adjacency, or restart-handoff work becomes visible.
- Caller-owned admission claims run before publication and can reject the
  admission atomically.

### Front Insertion

- The operator segment becomes the real and projected head.
- Existing survivors preserve relative order.
- A baked transition claim invalidated by insertion is discarded.
- Ordinary far-future audio is evicted before protected continuity or an
  earlier air-next promise.
- A new air-next item is rejected rather than evicting an existing air-next
  promise.
- Capacity never exceeds the configured maximum.

### Removal and Purge

- Removal targets stable identity, not a stale list position.
- Every removed segment settles discard accounting, listener-session claims,
  Home-fact reservations, release state, and Moment Receipts best-effort.
- Cleanup failure for one segment cannot prevent cleanup of that segment or
  later segments.
- Only discarded ephemeral files are removed; packaged assets survive.
- The final real queue determines Scaletta and tail adjacency.

### Playback

- Real dequeue and Scaletta consumption occur without an intervening await.
- Early playback rejection still settles discard state and unfinished work.
- Normal playback settles unfinished work once, regardless of EOF, skip, or
  file error.
- Dequeuing the head does not naively clear tail adjacency: the pulled segment
  remains the predecessor when the queue becomes empty.

### Continuity

- Candidate selection stays outside the queue module.
- Applying a prepared reservation is atomic across real queue, Scaletta, slot,
  epoch, discard settlement, and adjacency.
- Existing playable runway is never exchanged for less playable runway.
- Air-next entries and protected continuity are non-evictable by ordinary
  capacity pressure.
- An assetless replacement never cuts into silence.
- Listener-accepted bridge telemetry remains playback-owned.

## Correctness Fixes Kept Separate

The architecture commits must remain behavior-preserving. Confirmed defects are
fixed in separate `fix(queue)` slices with their own regression tests:

1. **Cleanup containment (`#852` plus front insertion).**
   A failing discard observer or file unlink cannot abort receipt settlement,
   remaining cleanup, or Scaletta reconstruction.
2. **Identity-correct drift and tail repair.**
   Runtime reconciliation rebuilds by queue ID; full and selective purges leave
   tail adjacency consistent with the real queue.
3. **Stop invalidation.**
   Stop invalidates capacity-blocked admissions even when Resume occurs before
   queue capacity becomes available.
4. **Restart-handoff protection (`#734`).**
   A startup-admitted path leaves the protected set on every terminal queue
   transition.

No architecture commit silently includes one of these behavior fixes.

## Commit Plan

Every commit below leaves the focused suite green. Commit types follow the
repository’s allowed Conventional Commit prefixes.

### Correctness Slice A: Cleanup

1. `chore(test): characterize queue cleanup continuation`
   - Add failing-observer and failing-unlink regression cases.
   - Prove every later segment still receives receipt and file cleanup.
2. `fix(queue): contain pre-air cleanup failures`
   - Contain failures per segment.
   - Preserve returned counts, queue order, and Scaletta truth.

### Correctness Slice B: Identity and Invalidation

3. `chore(test): characterize identity drift and stop races`
   - Add stale-head/real-tail drift reconstruction coverage.
   - Add a blocked-admission Stop→Resume race.
4. `fix(queue): repair Scaletta from stable identities`
   - Replace count truncation with identity-based reconstruction.
5. `fix(queue): invalidate blocked admissions on stop`
   - Advance the admission-invalidating generation synchronously with Stop.
   - Prove the blocked item never publishes after Resume.

### Architecture Cut 1: Owner and Transaction Core

6. `chore(queue): introduce the admitted audio owner`
   - Add the composition owner around the existing bounded queue.
   - Preserve the existing queue and Scaletta data representations.
7. `chore(queue): centralize queue identity and projection`
   - Move queue-ID and Scaletta-row construction behind the owner.
   - Keep temporary compatibility re-exports with identity guards.
8. `chore(queue): centralize drain rebuild and discard settlement`
   - Absorb the existing predicate-drop implementation.
   - Establish one task-accounting and cleanup path.
9. `chore(queue): route restart admission through the owner`
   - Remove startup’s producer-private import.
   - Preserve restart ordering, capacity, and protected-path behavior.

### Architecture Cut 2: Admission

10. `chore(queue): centralize tail admission`
    - Keep egress and stale-policy classification in producer code.
    - Move capacity waiting, final validity, publication, and exact retraction.
11. `chore(queue): centralize air-next insertion`
    - Move overflow, protected-priority, transition-claim, projection, and tail
      mechanics without changing policy.
12. `chore(queue): move tail bookkeeping behind admission`
    - Remove producer-private adjacency imports from startup and playback.
    - Preserve clean-source and fail-closed speech-bed behavior.

### Architecture Cut 3: Controls and Continuity Placement

13. `chore(queue): centralize removal and purge mechanics`
    - Route admin removal, blocklist removal, Home-fact removal, interrupt
      removal, full purge, and cutover replacement through the owner.
14. `chore(queue): centralize continuity placement`
    - Keep candidate selection outside.
    - Move protected capacity, slot, epoch, discard, projection, and adjacency
      mutation into one transaction.
15. `chore(queue): move control readers to immutable snapshots`
    - Replace private queue-storage reads in controls, status, and cache
      protection.

### Architecture Cut 4: Playback and Sealing

16. `chore(queue): acquire playback through queue leases`
    - Move real dequeue, Scaletta consumption/repair, and exactly-once
      completion behind the owner.
    - Keep last-mile eligibility and post-air behavior in playback.
17. `fix(queue): release terminal restart handoff protection`
    - Close `#734` through the now-central terminal lifecycle.
18. `ci(queue): enforce admitted queue ownership`
    - Reject raw Segment-queue mutation, private queue-storage access,
      `queued_segments` writes, and continuity-slot writes outside the owner.
    - Exclude listener byte fan-out queues explicitly.
19. `chore(docs): record admitted queue ownership`
    - Update architecture and repository maps.
    - Retire stale private-helper and queue-mutation descriptions.

## Delivery Slices

- Each correctness slice is its own PR.
- Each architecture cut is its own PR.
- Do not combine this train with the generic `streamer.py` split.
- Do not mix refactor moves and behavior fixes in one commit or PR.
- Pure moves require compatibility re-exports, facade-identity guards, whole
  repository patch-string search, and byte-faithfulness checks.
- Remove compatibility re-exports only in the sealing cut after all production
  callers have migrated.

At authoring time, do not merge this off-theme train into the active `2.18.0`
soak. Re-check release state before implementation; start the train only after
the current stable-cut decision.

## Testing Decisions

### Test Philosophy

- Test behavior through the admitted-audio interface.
- Do not assert private queue storage outside the owner’s focused tests.
- Test both sides of every admission/removal branch.
- Keep a structural guard for ownership; do not use structural assertions as a
  substitute for behavior tests.
- Every bug fix includes a regression that fails on the reviewed snapshot.

### Required Focused Suite

Run after every commit:

```bash
.venv/bin/pytest -q \
  tests/scheduling/test_queue_mutations.py \
  tests/scheduling/test_air_next.py \
  tests/scheduling/test_queue_commit_contract.py \
  tests/scheduling/test_egress_pipeline.py \
  tests/scheduling/test_restart_handoff_spool.py \
  tests/web/test_shadow_queue_sync.py \
  tests/web/test_streamer_routes.py \
  tests/web/test_streamer_routes_extended.py \
  tests/web/test_ui_control_contracts.py \
  tests/web/test_main.py \
  tests/integrations/test_now_playing_serializer.py \
  tests/integrations/test_now_playing_etag.py
```

The reviewed baseline for this set is 311 passing tests.

### Required New Guards

- Mixed-operation sequence: tail admit, air-next, selective remove, prepared
  continuity placement, consume, and purge.
- After every step, assert real order equals Scaletta ID order, slot exclusion,
  no duplicates, and balanced idle/join accounting.
- Cleanup continuation after observer and unlink failures.
- Identity-correct reconstruction with a stale shadow head.
- Capacity-blocked admission across Stop→Resume.
- Restart-handoff protection retirement on consume, remove, purge, and reject.
- Static ownership scan restricted to the Segment queue; listener byte queues
  remain valid.

### Full Validation Per PR

```bash
make check
bash scripts/check-release-invariants.sh
```

FFmpeg-marked tests are required only if a cut unexpectedly touches egress or
audio processing; such a dependency is a stop condition, not expected scope.

## Edge Proof and Rollout

No deploy, Edge cut, HA update, restart, stable promotion, or merge is
authorized by this specification.

When separately authorized, each architecture cut requires:

- PR checks green;
- exact-SHA Home Assistant add-on build green;
- deliberate Edge release for that cut;
- explicit Edge Pi update;
- first `/stream` byte within 2 seconds;
- healthy `/healthz`, `/readyz`, `/public-status`, and `/status`;
- no prolonged `503` or repeated queue-empty warnings;
- Scaletta order matching actual playback;
- trigger, non-head removal, purge/cutover, and restart proof with no duplicate,
  loss, ghost row, or orphan file;
- Player and Admin browser QA where behavior is visible.

The repository defines no fixed soak duration. Do not invent one. Stable
promotion remains a separate user-owned decision.

## Stop Conditions

Stop the current cut and redesign it if:

- it changes a public schema, operator workflow, listener order, or fallback
  policy;
- it requires an await or lock inside a compound queue mutation;
- it moves egress, rescue selection, HTTP, or post-air behavior into the queue
  module;
- it touches the currently airing segment through a queued-audio operation;
- it cannot preserve `join()` accounting and exactly-once completion;
- it requires a Home Assistant restart or live rollout without current-session
  authorization;
- its dependency closure pulls in the generic streamer or producer split.

## Out of Scope

- Live Queue drag-reorder.
- Play-soon semantics.
- Any implementation of issue `#410`.
- Reordering protected continuity or air-next rows.
- New HTTP routes or response fields.
- Listener or Admin UI changes.
- Public integration schema changes.
- Removal or redesign of `StationState`.
- Removal of the Scaletta compatibility projection.
- A custom queue transport.
- Multi-producer concurrency.
- Rescue-ladder redesign.
- Generic `streamer.py` or `producer.py` decomposition.
- Release, deployment, restart, merge, or stable promotion.

## Relationship to Existing Plans

This specification does not revive the archived producer-refactor train or the
generic streamer split.

It supersedes only the archived assumption that queue commit should be
producer-local. Current evidence shows startup, playback, and live controls are
real adapters of the same admitted-audio seam, so queue ownership belongs in
the scheduling layer as an independent deep module.

## Open Decisions

None for this behavior-preserving refactor.

Issue `#410` retains the future product decisions about reorder UX and protected
rows. Those decisions are intentionally deferred until this architecture has
shipped and soaked.
