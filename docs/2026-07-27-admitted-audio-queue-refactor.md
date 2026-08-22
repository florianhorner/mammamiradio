# Admitted-Audio Queue Refactor Specification

- **Status:** READY FOR IMPLEMENTATION
- **Decision date:** 2026-07-27
- **Reviewed snapshot:** `776d8cf7`
- **Merged onto `origin/main`:** `7e6deecd` (2026-08-18). Claims this snapshot overtook are corrected below.
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

v2.18.0 shipped on 8 August 2026 (`68da2ead`). The authoring-time soak gate is
lifted. Implementation may start; it still must not mix with a live audio-delivery
change or the generic `streamer.py` split.

### What `7e6deecd` already moved

A spec that describes a superseded design is worse than no spec. These claims
from the 27 July review are no longer true of `origin/main`:

- **Queue admission boundary.** `scheduling/queue_mutations.py` already owns
  drop/rebuild, unfinished-task accounting, moment-receipt settlement, and
  ephemeral unlink (and now calls `segment.release()`). The funnel remains
  `_enqueue_with_egress()` in `producer.py`, with the same three put fences
  (pre-egress, post-egress, post-capacity) plus caller-side
  `continuity_epoch` / `session_stopped` checks. Absorb the existing
  `queue_mutations` module; do not re-extract a second drain/filter/rebuild.
  The First Listen packaged mini-show never enters `asyncio.Queue[Segment]`
  and must stay outside this owner.
- **Restart handoff.** Spool write and boot admission now refuse Jamendo,
  starter, unknown sources, and add-on-external media (`require_known_source`
  at startup; source-kind allowlist on `_schedule_restart_handoff_spool`).
  Routing restart admission through the owner must preserve those filters,
  not the 27 July "any safe music" wording. Startup still imports
  `_queue_shadow_entry` from producer; that private import is still the cut.
- **Stop invalidation.** `#914` made Stop persist the session marker, then
  advance `continuity_epoch`, then purge. Paths that pass a continuity-aware
  `stale_check` into the funnel already retract a capacity-blocked item after
  Stop→Resume. Do not add a second admission-invalidating generation; reuse
  the epoch. The remaining work is proving the race on the funnel callers that
  already carry the fence, not extending it to the ones that deliberately lack
  it (see below).
- **Which callers carry the fence.** Measured at `7e6deecd`, not assumed. Every
  direct `_enqueue_with_egress` caller passes a `stale_check`
  (`producer.py:3219`, `:4496`, `:7205`, `:7226`), and the continuity bridge
  ladder passes `_bridge_stale_reason` at all four rungs (`producer.py:1103`,
  `:1133`, `:1167`, `:1199`) with per-rung re-arming, so there is no category of
  "inner bridge that omits `stale_check`". Three rescue paths omit it **on
  purpose**: the two quality-gate circuit-breaker inserts
  (`producer.py:5298`, `:5335`, both stamped `rescue: True`) and the playback
  gap fill (`streamer.py:4779`), whose own comment records the trade-off and
  says airing rescue audio is the safer default. Fencing any of the three
  removes the last audio between the listener and silence. Do not "fix" them as
  part of this train.

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
there is no single owner enforcing that invariant. Drop/rebuild now lives in
`queue_mutations.py`, yet admission, front-insert, retraction, Scaletta
publication, adjacency, and the continuity slot still sit in producer, startup,
and playback. Private producer helpers are imported by playback and startup,
and status code attempts to repair drift using counts rather than identity.

This lack of locality has already produced queue-shadow drift, repeated rescue
bugs, incomplete cleanup, stale-admission races, and an abstraction gap that
blocks safe Live Queue reorder.

## Goal and Success Criteria

The refactor is complete when:

- one module is the only production owner of the real Segment queue (the
  First Listen mini-show is not a Segment and stays outside that owner);
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
- Preserve the three existing funnel fences: before egress, after egress, and
  after a capacity wait. Also preserve the caller-side `session_stopped` and
  `continuity_epoch` checks that `#914` added around those fences.
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
  Moment Receipts;
- the First Listen packaged mini-show (client-local; never a `Segment`);
- restart-handoff *policy* (which source kinds may spool or boot-admit). The
  owner takes the already-validated segments and publishes them.

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
   Stop already advances `continuity_epoch` before queue cleanup (`#914`).
   Reuse that fence: a capacity-blocked admission that resumes after
   Stop→Resume must not publish. Do not invent a second generation counter.
   The gap today is proof, not coverage: no test combines a real capacity block
   with a genuine mid-wait epoch advance on a fenced caller. The three
   deliberately unfenced rescue paths stay unfenced.
4. **Restart-handoff protection (`#734`).**
   A startup-admitted path leaves the protected set on every terminal queue
   transition.

No architecture commit silently includes one of these behavior fixes.

## Commit Plan

Every commit below leaves the focused suite green, with one deliberate
exception: the three `chore(test):` characterization commits (1, 3, and 20) are
red until their paired `fix(queue):` commit lands, because a regression that
passes on the reviewed snapshot is not a regression (see Test Philosophy). Each
pair ships in one PR and may not be split across two. Commit types follow the
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
   - Add a blocked-admission Stop→Resume race against `continuity_epoch`.
4. `fix(queue): repair Scaletta from stable identities`
   - Replace count truncation with identity-based reconstruction.
5. `fix(queue): retract blocked admissions on stop via continuity epoch`
   - Prove a capacity-blocked item never publishes after Stop→Resume.
   - Reuse `continuity_epoch`; do not add a parallel generation counter.

### Architecture Cut 1: Owner and Transaction Core

6. `ci(queue): inventory admitted queue ownership violations`
   - Land the ownership scanner first, in **report-only** mode, so it measures
     every later cut instead of first running against twelve merged commits.
   - The scanner emits the inventory; this document does not hardcode line
     numbers that rot. Four categories, measured at `7e6deecd`:
     private `_queue` storage reads, **9** (producer 5, streamer 4);
     `queued_segments` writes, **12** (streamer 6, producer 4, main 1,
     queue_mutations 1); `continuity_slot` writes, **11** (streamer 9,
     producer 2); producer-private `_queue_shadow_entry` imports, **3**
     (`main.py:82`, `streamer.py:1510`, `:4362`). Commit the emitted baseline
     so later cuts diff against it.
   - Match attribute writes (`state.queued_segments = ...`), not bare-name
     assignment. A loose pattern flags the local `queued_segments = tuple(...)`
     at `integrations/now_playing.py:98`, which is a read-only consumer inside
     the **frozen** v1 contract surface. That file cannot be edited to satisfy
     a guard, so a false positive there is a hard block, not a nit. Exclude
     `mammamiradio/integrations/**` explicitly.
   - Private `_queue` storage is reached through `getattr(q, "_queue", ())`,
     not literal attribute access, so the scan must match both forms.
   - Every later cut deletes entries from the inventory. Growing it is a
     review failure, not an allowlist entry.
   - Exclude listener byte fan-out queues explicitly.
7. `chore(queue): introduce the admitted audio owner`
   - Add the composition owner around the existing bounded queue.
   - Preserve the existing queue and Scaletta data representations.
8. `chore(queue): centralize queue identity and projection`
   - Move queue-ID and Scaletta-row construction behind the owner.
   - Retire the two `_queue_shadow_entry` imports in `streamer.py` (`:1510`
     feeding `:1523`, and `:4362` feeding `:4365`). They are Scaletta-row
     construction, so they belong to this commit, not to the restart or
     controls cuts.
   - Keep temporary compatibility re-exports with identity guards.
9. `chore(queue): centralize drain rebuild and discard settlement`
   - Absorb `scheduling/queue_mutations.py` (the existing predicate-drop
     owner) rather than re-extracting drain/filter/rebuild from producer.
   - `scheduling/queue_mutations.py` stays as a compatibility re-export until
     the sealing cut. Do not delete it here.
   - Rename `tests/scheduling/test_queue_mutations.py` to the owner's test path
     in this same commit, and update the Required Focused Suite list below in
     the same commit. Any cut that moves a test file re-pins that list.
   - Establish one task-accounting and cleanup path.
10. `chore(queue): route restart admission through the owner`
    - Remove startup's producer-private `_queue_shadow_entry` import
      (`main.py:82`), the last one left after commit 8.
    - Preserve restart ordering, capacity, protected-path behavior, and the
      post-2.18 source filters: no Jamendo, no starter, no unknown source, no
      add-on-external media.

### Architecture Cut 2: Admission

11. `chore(queue): centralize tail admission`
    - Keep egress and stale-policy classification in producer code.
    - Move capacity waiting, final validity, publication, and exact retraction.
    - The three fences straddle the unchanged `_apply_egress()` call.
      Re-anchoring them around that call is expected mechanical work and is not
      the "touches egress" stop condition; moving egress *policy* into the
      queue module is.
12. `chore(queue): centralize air-next insertion`
    - Move overflow, protected-priority, transition-claim, projection, and tail
      mechanics without changing policy.
13. `chore(queue): move tail bookkeeping behind admission`
    - Remove producer-private adjacency imports from startup and playback.
    - Preserve clean-source and fail-closed speech-bed behavior.

### Architecture Cut 3: Controls and Continuity Placement

14. `chore(queue): centralize removal and purge mechanics`
    - Route admin removal, blocklist removal, Home-fact removal, interrupt
      removal, full purge, and cutover replacement through the owner.
15. `chore(queue): centralize continuity placement`
    - Keep candidate selection outside.
    - Move protected capacity, slot, epoch, discard, projection, and adjacency
      mutation into one transaction.
    - Retire the ten direct `state.continuity_slot` writes
      (`streamer.py:1388`, `:1405`, `:1407`, `:1701`, `:1818`, `:1880`,
      `:1883`, `:4798`, `:7182`; `producer.py:2274`).
16. `chore(queue): move control readers to immutable snapshots`
    - Replace private queue-storage reads in controls, status, and cache
      protection.

### Architecture Cut 4: Playback and Sealing

17. `chore(queue): acquire playback through queue leases`
    - Move real dequeue, Scaletta consumption/repair, and exactly-once
      completion behind the owner.
    - Keep last-mile eligibility and post-air behavior in playback.
18. `ci(queue): enforce admitted queue ownership`
    - Flip commit 6's scanner from report-only to blocking with an empty
      inventory. If the inventory is not empty, an earlier cut is unfinished;
      finish it rather than widening the allowlist.
19. `chore(docs): record admitted queue ownership`
    - Update architecture and repository maps.
    - Retire stale private-helper and queue-mutation descriptions.
    - Remove the compatibility re-exports, including
      `scheduling/queue_mutations.py`.

### Correctness Slice C: Restart Handoff

Ships after cut 4, because closing `#734` needs the central terminal lifecycle
that commit 17 introduces. It is a behavior change and therefore its own PR,
never folded into the sealing cut.

20. `chore(test): characterize restart-handoff protection retirement`
    - `state.restart_handoff_admitted_paths` is only ever added to
      (`main.py:317`, `:319`) and read for cache protection
      (`producer.py:1989`). Nothing removes an entry today, so this test is red
      until commit 21.
21. `fix(queue): release terminal restart handoff protection`
    - Close `#734`: leave the protected set on consume, remove, purge, and
      reject.

## Delivery Slices

- Each correctness slice is its own PR.
- Each architecture cut is its own PR.
- Do not combine this train with the generic `streamer.py` split.
- Do not mix refactor moves and behavior fixes in one commit or PR.
- Enforce that mechanically at PR time: if a cut's commit list contains a `fix(`
  prefix, re-split it before opening the PR. A refactor-labelled PR carrying a
  behavior change is also the shape that qualifies for the "pure internal
  refactor" QA skip, so the mislabel costs the QA run too.
- Pure moves require compatibility re-exports, facade-identity guards, whole
  repository patch-string search, and byte-faithfulness checks.
- Remove compatibility re-exports only in the sealing cut after all production
  callers have migrated.

v2.18.0 has shipped. Re-check that no overlapping live-audio branch still
holds the producer/streamer/handoff files before starting a cut.

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
  tests/scheduling/test_producer_unit.py \
  tests/web/test_shadow_queue_sync.py \
  tests/web/test_streamer_routes.py \
  tests/web/test_streamer_routes_extended.py \
  tests/web/test_ui_control_contracts.py \
  tests/web/test_interrupt_endpoint.py \
  tests/web/test_playlist_purge.py \
  tests/web/test_song_blocklist.py \
  tests/web/test_main.py \
  tests/integrations/test_now_playing_serializer.py \
  tests/integrations/test_now_playing_etag.py
```

Four paths were added to the 27 July list after checking what actually exercises
the code being moved. `test_producer_unit.py` imports and calls
`_enqueue_with_egress` directly, including its `stale_check` branch, and is the
largest direct test of the funnel this train relocates; omitting it left cuts 2
and 4 ungated. `test_interrupt_endpoint.py`, `test_playlist_purge.py`, and
`test_song_blocklist.py` cover the interrupt, purge, and blocklist removal paths
that commit 14 routes through the owner.

The reviewed baseline was 311 passing tests at `776d8cf7` against the shorter
27 July list. At `7e6deecd` the list above collects **1560** tests. Re-pin the
passing count on the first implementation commit rather than carrying either
number forward, and re-pin it again in commit 9, which renames a file in this
list.

### Required New Guards

- Mixed-operation sequence: tail admit, air-next, selective remove, prepared
  continuity placement, consume, and purge.
- After every step, assert real order equals Scaletta ID order, slot exclusion,
  no duplicates, and balanced idle/join accounting.
- Cleanup continuation after observer and unlink failures.
- Identity-correct reconstruction with a stale shadow head.
- Capacity-blocked admission across Stop→Resume, fenced by `continuity_epoch`.
  Existing coverage is half of this and should be extended, not duplicated:
  `test_queue_commit_contract.py::test_blocked_queue_put_retracts_segment_if_it_becomes_stale_before_capacity`
  blocks on a full queue but injects the stale reason directly, and
  `::test_prewarm_discards_after_stop_resume_aba_during_render` performs a real
  epoch advance but never fills the queue. The missing guard is one test with
  both halves. The three deliberately unfenced rescue paths are out of scope.
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

## Backout

Stop Conditions above cover a cut still in flight. This covers a cut that passed
its Edge proof and turned out wrong afterwards.

- **Before the next cut merges**, a bad cut is revertible as one whole PR. That
  is the reason each cut is its own PR and why compatibility re-exports survive
  until the sealing cut: the revert window is exactly the interval in which no
  later cut has built on the reverted seam yet.
- **After the sealing cut** (commit 19) removes the re-exports, a bad cut is
  corrected forward, not reverted. Plan accordingly before merging 19.
- **Recovery on the Edge Pi is an add-on version pin to the previous Edge tag.**
  Never a container-level change, a `docker cp`, or a process signal. Leadership
  principle #3 has no exception for a refactor the maintainer regrets, and a
  pinned downgrade is one planned restart against three unplanned drops.
- A backout is a listener-visible event. It gets the same audio proof as a
  rollout: first `/stream` byte within 2 seconds, Scaletta matching playback,
  no repeated queue-empty warnings.

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
- The First Listen packaged mini-show (client-local; never a `Segment`).
- Restart-handoff source-kind policy (Jamendo, starter, unknown, add-on-external).
- Release, deployment, restart, merge, or stable promotion.

## Relationship to Existing Plans

This specification does not revive the archived producer-refactor train or the
generic streamer split.

It supersedes only the archived assumption that queue commit should be
producer-local. Current evidence shows startup, playback, and live controls are
real adapters of the same admitted-audio seam, so queue ownership belongs in
the scheduling layer as an independent deep module.

## Open Decisions

Whether the playback gap fill (`streamer.py:4779`, `_stamp_playback_gap_fill`)
should yield to a fresher timeline after its bounded probe. It stamps the
current epoch onto the fill, which makes the staleness gate below it
unreachable, so a control that queued fresh runway mid-probe has its cut
deferred until the fill ends. That is a recorded trade-off, not an oversight.

This is **not** a queue-ownership question and it is not scheduled in this
train. Changing it requires the full three-scenario audio-delivery test set
(normal, empty fallback, post-restart) from `CLAUDE.md` first, and it must never
remove the bridge ladder's per-rung re-arming. The two quality-gate
circuit-breaker inserts (`producer.py:5298`, `:5335`) stay unfenced for the same
reason. Leave all three alone while moving queue ownership.

Issue `#410` retains the future product decisions about reorder UX and protected
rows. Those decisions are intentionally deferred until this architecture has
shipped and soaked.
