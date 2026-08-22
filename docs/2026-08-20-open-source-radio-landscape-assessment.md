# ADR: retain the FastAPI-owned radio core

> **Status:** Accepted
> **Date:** 2026-08-21
> **Scope:** Architecture decision only; no runtime behavior changes are
> authorized by this document.

## Context

Mamma Mi Radio is packaged as a small, household-oriented radio appliance,
normally deployed through Home Assistant or Docker. FastAPI owns its HTTP
surfaces, producer and playback loops run as application-managed asynchronous
tasks, and durable state remains local.

A landscape review compared this design with seven open-source radio systems.
Those systems demonstrate the value of a separate playout engine for larger
operational shapes, but they do not establish that Mamma Mi Radio needs that
boundary today.

## Decision

Retain the FastAPI-owned radio core.

Do not introduce Liquidsoap, Icecast, HLS delivery, a message broker, or a
distributed controller/streaming architecture without a measured requirement
that the current design cannot satisfy.

Reliability work should strengthen explicit ownership and failure handling
inside the existing appliance. It must remain separate from listener-audible
fallback policy and from the behavior-preserving admitted-audio queue refactor.

## Rationale

The current architecture matches the supported product shape:

- one station per installation;
- one Home Assistant or Docker appliance;
- one shared live-timeline delivery owner, apart from bounded per-client startup
  audio;
- local SQLite and file-backed state;
- a small trust boundary around privacy-sensitive Home context.

A separate playout or distribution layer would add another process boundary,
configuration surface, deployment path, and recovery model. That cost becomes
worthwhile only when an approved requirement demands multiple stations,
multiple delivery formats, stronger isolation, or higher measured fan-out.

## Architectural invariants

1. `run_playback_loop()` remains the sole owner of listener-byte delivery through
   `LiveStreamHub.broadcast()`.
2. Admitted-audio queue mutation and listener-byte delivery are distinct
   responsibilities. The behavior-preserving queue work remains documented in
   `docs/2026-07-27-admitted-audio-queue-refactor.md`.
3. The FastAPI lifespan is the only owner allowed to create and retire core
   producer and playback tasks.
4. A future lifecycle change must distinguish intentional shutdown from
   unexpected termination and must not create replacement core tasks inside
   uncertain shared state.
5. Health routes may reconcile bounded diagnostic projections, but they do not
   own process or task restart actions.
6. Listener-audible fallback behavior changes only through a separate,
   explicitly reviewed workstream.
7. Restart handoff, continuity epochs, queue projection, and stale-writer fencing
   must remain consistent across lifecycle changes.
8. Recovery claims require observed evidence from every supported deployment
   mode; an HTTP response alone is not proof of process recovery.

## Public interfaces and privacy

`/healthz` is the liveness probe. `/readyz` is the stricter listener-service
readiness probe. This ADR does not change their current responses. The restricted
`/status` operator surface provides additional detail under the configured access
policy.

Probe evolution must remain additive. A breaking probe change requires its own
explicit, separately reviewed compatibility decision. Any new unauthenticated
health-probe detail must use an explicit allowlist of bounded values. New fields
must not expose exception text, tracebacks, filesystem paths, credentials,
prompts, provider payloads, or raw Home Assistant state. Existing deliberately
sanitized listener-facing projections are outside this probe contract.

Operational evidence can contain environment metadata. Raw evidence remains
outside the public repository by default. Only deliberately sanitized,
schema-validated examples or aggregates may be proposed for publication.

## Follow-up boundaries

| Workstream | Purpose | Boundary |
|---|---|---|
| Admitted-audio ownership | Establish one queue-mutation owner while preserving behavior. | Independent implementation train. |
| Lifecycle health | Make core-task state and terminal behavior explicit. | Deployment recovery ownership must be verified first. |
| Fallback policy | Inventory and simplify listener-audible recovery selection. | No incidental behavior change during lifecycle or queue work. |
| Audio configuration | Validate encoding, pacing, and metadata consistency. | Independent correctness work. |
| Capacity evidence | Measure fan-out on supported hardware. | Begins only after an approved target defines success. |

Each workstream requires its own plan, regression coverage, and evidence
appropriate to the claim it makes.

## Consequences

Benefits:

- Deployment remains small and understandable.
- Existing FastAPI, queue, handoff, and fan-out code stays reusable.
- Lifecycle reliability can improve without silently changing the station's
  sound.
- Migration decisions become evidence-driven.

Costs and risks:

- Producer and playback still share one process and failure domain.
- Correct recovery depends on deployment-specific behavior that must be
  established before implementation.
- Lifecycle changes must coordinate carefully with queue ownership and restart
  handoff.
- Future scale or delivery-format requirements may still justify another
  service boundary.

## Non-goals

This decision does not:

- implement lifecycle supervision or change health responses;
- change fallback ordering or listener-facing audio;
- authorize deployment tests against a live household system;
- define a capacity target;
- add a delivery format or external playout engine;
- publish operational receipts or internal review material.

## Migration triggers

Reconsider the architecture only when at least one trigger has an approved
requirement and reproducible evidence:

| Trigger | Evidence needed |
|---|---|
| Multiple independent stations | Concurrent stations require independent schedules and failure domains. |
| Multiple delivery formats or mounts | A committed integration cannot be served by the existing MP3 stream. |
| Fan-out capacity | The appliance fails an approved listener, hardware, duration, latency, resource, or drop budget. |
| Crash isolation | A reproduced incident shows whole-process recovery cannot meet an approved objective. |
| Scheduling complexity | An approved requirement cannot remain explicit and maintainable in the in-process scheduler. |

## Public sources

The comparison reviewed these projects at pinned revisions:

| Project | Revision |
|---|---|
| SUB/WAVE | [`c0d8d61`](https://github.com/perminder-klair/subwave/tree/c0d8d613a4b29adc659d8ca2bedc0a98163f3841) |
| Savonet AI Radio | [`6718b96`](https://github.com/savonet/ai-radio/tree/6718b96e7983b2d179b1015048a99e447a0a042d) |
| Claudio FM | [`4939b1a`](https://github.com/bingyanglu/Claudio-FM/tree/4939b1adc5ddc5ed1289a1e93cd0235b98634abe) |
| Codio Local AI Radio | [`356b769`](https://github.com/immortalyubai/codio-local-ai-radio/tree/356b7694bcb4a9915c9f8b12aab03decb39a15ba) |
| AzuraCast | [`6951e0e`](https://github.com/AzuraCast/AzuraCast/tree/6951e0e1e2972081b0d9f3ebe9ba98b4cc5cf3c2) |
| LibreTime | [`d5050be`](https://github.com/libretime/libretime/tree/d5050be16be68131876218420285bc0c67a6b960) |
| Tomato | [`dbbc72e`](https://github.com/dtcooper/tomato/tree/dbbc72e6eff1c7f389612d25722d5e282bbd5680) |

Lifecycle design should follow the official
[FastAPI lifespan documentation](https://fastapi.tiangolo.com/advanced/events/)
and Python
[`asyncio` task and cancellation documentation](https://docs.python.org/3.11/library/asyncio-task.html).

## Implementation status

This ADR changes documentation only. Future implementation remains separately
scoped and reviewed.
