# Source Truthfulness Plan — Architectural Fix for Repeated Label-Drift Bugs

**Date:** 2026-05-02
**Trigger:** PR #281 draft hold-back. Two operator-honesty bugs of the same class (`PlaylistSource.kind` ≠ actual track origins) shipped successively despite three Claude review agents, the cathedral, 1,590 tests, and the original code review. Florian's call: "we don't release. 2 impossible errors breaking the rules. something is fundamentally off."
**Council:** Local 5-lens parallel investigation (type system / architecture / runtime invariant / domain modeling / operator experience). All five lenses converged on the same root cause from different framings. Full output: `.context/research/2026-05-02-council-playlist-source-truthfulness.md`.

---

## Root cause (one sentence)

`PlaylistSource.kind` is structurally orphaned from the `list[Track]` it describes — labels are minted by callers downstream of loaders, no boundary forces the question, no cross-check surfaces drift, and no test enforces that `Track.source` values actually match the declared `kind`.

## The five lenses (one-line each)

| Lens | Diagnosis |
|------|-----------|
| Type system | Label is structurally orphaned from the payload |
| Architecture | Module conflates source acquisition and source resolution |
| Runtime invariant | Loaded track origins must equal source.kind by construction |
| Domain modeling | PlaylistSource conflates intent / resolution / persistence |
| Operator-experience | kind tries to answer four questions with no cross-check |

All point at the same root.

## What the council recommends — three layers

### Layer A: Prevention (make the bug structurally impossible)

1. **Per-loader self-labeling.** Each tier (`charts`, `jamendo`, `local`, `demo_asset`, `demo_builtin`) becomes a pure `Config -> Optional[(tracks, PlaylistSource)]`. Caller cannot re-label. `_load_chart_source_tracks`'s in-place mutation of `chart_tracks` and `_charts_source(len(...))` stamping go away. Single resolver walks the chain.
2. **Discriminated union of per-kind source classes that own their tracks.** `ChartsSource | JamendoSource | LocalSource | DemoSource`, each with `kind: Literal["..."]` and a `tracks: tuple[Track, ...]` field. `__post_init__` enforces `t.source ∈ allowed_origins`. Pydantic v2 `Field(discriminator="kind")` for free JSON round-trip.
3. **One vocabulary.** Replace the `Track.source` / `PlaylistSource.kind` drift (`youtube` vs `charts`, no cross-mapping) with a single `Origin` enum. Both fields agree by construction, never by convention.

### Layer B: Detection (catch what slips through prevention)

4. **Parametrized invariant test** (`tests/playlist/test_source_kind_invariant.py`, paste-ready in the council artifact): registers each loader, asserts `t.source == KIND_TO_TRACK_SOURCE[source.kind]` for every track. New loaders register by adding one tuple. **Would have failed both shipped bugs.**
5. **`source_truthfulness` runtime telemetry.** Add a block to `_public_status_payload` showing `declared_kind`, `track_origins` (Counter over actual `t.source`), `mismatch: bool`. Surface in admin Engine Room. Both shipped bugs would have shown `mismatch: true` on the first `/status` poll.
6. **Boot-summary log line.** Replace four scattered `logger.info` calls in `fetch_startup_playlist` with one structured line: `Boot playlist: declared=local, loaders=[local], tracks=23 (local:23)`. CI grep + log scraping spot drift.

### Layer C: Detection-of-detection (CI must not lie either)

7. **Fix the CI swallow.** `scripts/coverage-ratchet.py:run_coverage()` only fails when `result.returncode != 0 AND not modules`. When pytest fails tests but emits per-file coverage, `modules` populates and the failure is masked. **Without this fix, every other change here is theatre — green builds while red tests is worse than no test.** Two-line patch in the script + add a dedicated `pytest tests/` step in `quality.yml` that runs *before* the coverage ratchet.

### Layer D: Future / optional

8. **`Resolution = Pure(origin, tracks) | Blended(primary, enrichments, tracks)` ADT.** The current model has no slot for "charts, enriched with 12 local tracks" — so it lies as `kind="charts"`. An honest `Blended` lets the dashboard say "Italian charts, blended with 12 of your tracks." Honesty as a feature, not a chore.
9. **`mutmut` on `mammamiradio/playlist/`, nightly only.** Mutation testing catches both shipped bugs. ~5-10 min nightly. Don't run on every PR.

---

## Sequencing

### **Tonight / next session — non-negotiable:**

- **Fix CI swallow first** (item 7). Without it, the rest of this plan can't be verified. Two-line patch + a dedicated `pytest tests/` step in `quality.yml` that runs before the coverage ratchet. Tiny PR, ships immediately.

### **Next focused work block (1 session, ~2-3 hours):**

- Add parametrized invariant test (item 4). One file. Catches both shipped bugs.
- Add `source_truthfulness` block to `_public_status_payload` (item 5). Counter + Boolean, no new persistence.
- Resurrect PR #281 with these two additions stacked on top of the existing fixes. Land.

### **Architectural work (1-2 sessions, can be deferred but not by long):**

- Per-loader self-labeling refactor (items 1 + 2 + 3). ~80 LOC delta in `mammamiradio/playlist/playlist.py`. Public signatures of `fetch_startup_playlist` and `load_explicit_source` stay identical. Tests reference `_merge_local_music_tracks` / `_load_chart_source_tracks` — search-and-update. The local-on-charts enrichment becomes an explicit `_with_local_blend(loader)` decorator returning a `local`-blended source label, never a charts label.

### **Future (1+ release):**

- `Resolution = Pure | Blended` ADT (item 8). Product upgrade — admin UI gains "Charts, blended with N of your tracks."
- `mutmut` nightly (item 9). Quality dashboard.

---

## What this plan is NOT

- It is not a rewrite. The architectural refactor is ~80 LOC delta in one file. Public signatures unchanged.
- It is not "bigger PRs." The first three items can ship as three small PRs over a week.
- It is not deferring the fix. Items 1-7 belong in the immediate roadmap; PR #281 doesn't merge until at least items 4 + 5 + 7 are in.

## What this plan IS

- A formal admission that the **single-string `kind` field with no construction-time invariant** is the root cause of both shipped bugs and would generate more
- A concrete prescription — paste-ready test, concrete type signature, concrete refactor shape — derived from five independent lenses converging
- A pacing that's compatible with the "Release BETTER not MORE" rule: detection layer (4 + 5 + 7) ships first; prevention layer (1 + 2 + 3) ships when it has a quiet window; ADT upgrade (8) ships when there's a real product story for it

## Acceptance criteria

PR #281 (or its successor) merges only when all of these are true:
- [ ] CI swallow fixed; pytest test failures fail the run
- [ ] Parametrized invariant test green and registered in `tests/playlist/test_source_kind_invariant.py`
- [ ] `source_truthfulness` block visible in `/status` and admin Engine Room
- [ ] No new operator-honesty failure observable in `/status` after a fresh boot with each of: `(allow_ytdlp=False, jamendo=missing, music/=empty)`, `(allow_ytdlp=True, charts API empty, music/=present)`, `(allow_ytdlp=False, music/=present)`, `(jamendo configured, returns empty)`

After the architectural refactor lands:
- [ ] `_load_chart_source_tracks` and `_merge_local_music_tracks` no longer exist as separate functions
- [ ] `fetch_startup_playlist` and `load_explicit_source` share a single resolver
- [ ] Construction of any source class with mismatched `t.source` raises `ValueError` at the boundary

## Confidence

Very high. Five independent lenses, five framings, full convergence on root cause. They differ on prescription *depth*, not direction. The codex catch on PR #281 (a *fourth* lens beyond the original three Claude squad agents) is itself evidence the bug class is detection-resistant; the prescription includes both prevention and detection layers because either alone has been demonstrably insufficient.

## References

- Council artifact: `.context/research/2026-05-02-council-playlist-source-truthfulness.md`
- PR #281 (in draft, holding for this work): https://github.com/florianhorner/mammamiradio/pull/281
- Codex-bot finding doc: `docs/2026-04-28-codebase-issues-task-vorschlaege.md`
- Cathedral plan precedent (multi-PR architectural work): `docs/2026-04-28-cathedral-restructure.md`
- Doc-sweep recipe: `docs/2026-04-28-post-cathedral-doc-sweep.md`

---

# Iteration 2 — appended 2026-05-02

The first iteration nailed the root cause; iteration 2 widened the lens. Five new agents in parallel: codebase audit (other instances), prescription stress-test, industry patterns, concrete Pydantic-vs-dataclass implementation spec, mutation testing setup. Full output appended in `.context/research/2026-05-02-council-playlist-source-truthfulness.md`. Material shifts to the plan below.

## Shift 1 — Scope expands. Same pattern lives in 5 hot spots.

The cathedral didn't shrink the bug class — it spread it across naves. Audit found the same `kind: str` / `type: str` / `role: str` pattern in:

| Hot spot | Severity | Why |
|---|---|---|
| `PlaylistSource.kind` | P0 (already shipped twice) | Known |
| `Segment.metadata["audio_source"]` | **P0 (next #280)** | 5 writers in `producer.py`, 1 fallback reader in `streamer.py:225-242`, surfaces via `/api/runtime/health` and listener UI. Free text. Typo or new code path = lying telemetry. |
| `Segment.metadata["type"]` (vs `Segment.type: SegmentType`) | P1 | Free text in metadata, enum in dataclass — they can disagree. |
| `AdPart.type: str` (`voice/sfx/pause/environment`) | P1 | `synthesize_ad` branches on this. Typo silently dropped. |
| `AdVoice.role` / `AdPart.role` | P1 | `_cast_voices` looks up `role_index`. Mismatch falls through to random voice. Listener-audible: "label says hammer, content is seductress." |
| `script.format` (`AdScript.format`) | P1 | LLM-generated string. Not validated against `AdFormat` enum. Dashboard shows whatever the LLM wrote. |
| `HostPersonality.engine: str` (`edge/openai`) | P2 | Internal only, but typo silently picks `edge`. |

**Plan implication:** the prescription must be parameterized over "any label-content discriminator," not just `PlaylistSource`. The Layer-A+B mechanisms (discriminated union + invariant test + truthfulness telemetry) are the *recipe*; apply once for `PlaylistSource`, then walk the table above and apply where severity warrants.

**Recommended addition:** a `Segment.__post_init__` invariant that MUSIC segments must declare a non-empty `audio_source` from a `StrEnum`. Bundles with the `PlaylistSource` work since both touch the same producer/streamer surface.

## Shift 2 — Sequencing was wrong. Layer 5 ↔ Layer 8 are inseparable.

Stress-test agent's killer finding: **Layer 5 (`source_truthfulness` runtime telemetry) cannot ship before Layer 8 (`Resolution = Pure | Blended` ADT)** without crying wolf in production. The most common production path — `allow_ytdlp=True` + charts non-empty + `music/` populated — is legitimate `charts+local enrichment`. Without `Blended`, telemetry reports `track_origins={"youtube":50,"local":12}, declared_kind:"charts", mismatch:True`. Engine Room shows a permanent red flag → operators learn to ignore it → signal destroyed.

**Two ways out:**

- **Option A (defer telemetry):** Ship Layer 4 (parametrized invariant test) + Layer 7 (CI swallow fix) + Layer 1-2 (per-loader self-labeling + discriminated union). Hold Layer 5 until Layer 8 ships together. Honest about the timeline.
- **Option B (relaxed telemetry):** Ship Layer 5 with the predicate "kind absent from origins" (not "any extra origins present"). The legit charts+local case has `youtube` in origins, so `mismatch=False`. This narrows what the telemetry catches but avoids false positives.

**Recommendation: Option A.** Option B's relaxed predicate fails to catch a charts-source returning ONLY `local` origins (the actual codex catch from PR #281's commit `352bf77`). Wait for `Blended`.

## Shift 3 — Six concrete gaps in the original prescription

| Gap | Where | Fix |
|---|---|---|
| `fetch_chart_refresh()` is a sibling chart loader the prescription doesn't address | `playlist.py:553` | Either register it in the parametrized invariant test or kill it in favor of the unified resolver |
| `kind="url"` branch labels arbitrary URLs as charts | `playlist.py:469`, `streamer.py:1952` | After-refactor: `PlaylistSource` becomes a *transport* DTO for inbound `kind="url"` requests; `ResolvedSource` is the discriminated union for resolved sources. `__post_init__` validates `url` matches `kind` (e.g., `JamendoSource.url` must start `jamendo://`). |
| `Track.source` defaults to `"youtube"` in `models.py:49` — `Track(**asdict(t))` strips and re-adds `youtube` | `models.py:49` | Make `source` non-defaulted; missing → raise. |
| Demo split is cosmetic — `Track.source="demo"` covers both `demo_asset` and `demo_builtin` `PlaylistSource` kinds | `models.py`, `playlist.py` | Split `Track.source` literal to `demo_asset` / `demo_builtin` OR collapse the two source kinds into one. Either way, sync the vocabularies. |
| Persistence writes before truthfulness verification | `streamer.py:1965` | Reorder: verify → persist. Or: never persist a source whose tracks failed truthfulness. |
| Race: `state.playlist.append()` from listener-request downloads can mutate between resolve and persist | `streamer.py:1858, 1906` | Existing `source_switch_lock` covers `_apply_loaded_source` but not the appends. Extend lock or move appends into a queue. (Severity: low — track_count drift is benign and self-corrects on next boot.) |

**Plan implication:** four of these six are "while-you're-here" additions to the discriminated-union refactor PR. The other two (race, persistence ordering) ship as small follow-up patches.

## Shift 4 — Implementation is cheaper than the council assumed

Implementation-spec agent's audit:

- **`PlaylistSource` is a mutable dataclass, NOT Pydantic.** Zero `response_model` references. JSON exits via `_serialize_source()` (`streamer.py:307-317`) returning a dict. **No Pydantic dependency to wire up.**
- **`src.track_count = len(tracks)` post-construction mutation** at `playlist.py:449, 545` is the *very pattern* that lets the bug exist. Frozen dataclasses block this — construction must take `tracks` at the boundary. **That's the safety mechanism — exactly what we want.**
- **Phased PR shape:**
  - Phase 1 (additive, ~60 LOC): define `ChartsSource | JamendoSource | LocalSource | DemoSource` in `models.py` alongside existing `PlaylistSource`. `__post_init__` enforces allowed `Track.source`.
  - Phase 2 (~25 LOC delta): switch the four factories to take `tracks`, return variants. `load_explicit_source` and `fetch_startup_playlist` return the union. Drop the `.track_count` post-mutation.
  - Phase 3: `read_persisted_source` dispatches dict via `_VARIANT_BY_KIND`. `_serialize_source` unchanged. JSON wire shape stable.
  - Phase 4: remove old type, narrow ~26 test sites (mostly sed). 11 test files.
- **Net delta: ~140 LOC, single PR, public JSON shape unchanged, no Pydantic added.** The original plan said ~80 LOC; the extra ~60 is the four variant classes and their validators (the actual safety surface).

**The merge problem decision:** The relaxed predicate (`ChartsSource` allows `t.source ∈ {"youtube","local"}`) IS the right call until `Blended` ADT lands. Strict (`{"youtube"}` only) would crash on every boot where `music/` has files — which is Florian's Pi setup. TODO marker + tighten when `Blended` ships.

## Shift 5 — Mutmut + invariant test is the killer combo

- Mutmut alone caps at ~40% on label-content drift modules.
- Invariant test alone catches the two known bugs but might not catch *future* drift in subtly different code paths.
- **Combined:** both shipped bugs would have died as mutants. Future label-drift refactors that don't kill the same mutants surface as "new survived mutants" in the nightly job's PR comment.

Concrete spec (paste-ready in the council artifact): mutmut 2.5+ scoped to `mammamiradio/playlist/playlist.py`, ~561 LOC. Nightly job at 03:00 UTC. ~12-18 min cold, **2-4 min warm with cache**. `.mutmut-baseline.json` + `scripts/mutmut-ratchet.py` mirroring `coverage-ratchet.py`. Auto-commits new baseline on green main; opens an issue on regression.

## Shift 6 — Industry gold-standard recipe converges with Layer 1+2+3

Industry-patterns agent surveyed Beets, MusicBrainz Picard, MPD, Avro, Pydantic v2 in Anthropic/OpenAI SDKs.

**Convergence:**
- All: tag at construction, never inferred later
- Most (MPD, Avro, Pydantic SDKs): dispatch by tag — handler and tag are bound at registration
- MPD's source-as-handler is the gold standard: each `InputPlugin` registers its `name` + supported URI schemes; dispatch is exclusive. **Mislabeling becomes structurally impossible at registration time.**

**Beyond mammamiradio's planned scope:** industry pattern goes further than the council's prescription by discriminating at the **Track level too**, not just `PlaylistSource`. A `LocalTrack(path: FilePath)` cannot be constructed with a YouTube URL because Pydantic's `FilePath` rejects URLs. The bug from PR #281 commit `352bf77` would have been a 1-line `ValidationError` instead of a runtime drift shipped to users.

**Recommendation:** PlaylistSource discrimination first (cheap, ~140 LOC, this PR). Track-level discrimination is a Phase 5+ architectural step (much bigger refactor, cascades into ~30 files). The PlaylistSource refactor with `__post_init__` validating `t.source ∈ allowed` gives you most of the protection at much less cost than per-track classes.

## Revised sequencing

The original four-phase sequencing stands but with these refinements:

### **Tonight, ~10 min:** CI swallow fix (still non-negotiable, still first)

`scripts/coverage-ratchet.py:72-74` two-line patch + dedicated `pytest tests/` step in `quality.yml` running before the coverage ratchet. Without this, every other change is theatre. Tiny PR.

### **Next focused work block (1 session, ~3-4 hours):** the operator-honesty PR

Bundle:
- Parametrized invariant test (Lens 3 paste-ready), registered for all four loaders + `fetch_chart_refresh`
- Discriminated union refactor (Phase 1 + 2 + 3 from impl-spec): ~140 LOC, no Pydantic
- `Track.source` non-defaulted (gap fix #3)
- `read_persisted_source` validates against the union; corrupt → log + delete (gap fix #5)
- Resurrect PR #281 stacked on top of these. Land.

### **NOT in the same PR (per Shift 2):** Layer 5 truthfulness telemetry

Holds until Layer 8 (`Blended` ADT) ships in a follow-up. Otherwise cries wolf.

### **Follow-up PR (1-2 sessions):** `Resolution = Pure | Blended` ADT + telemetry

- Introduce `Resolution` at the loader boundary (per Lens 4)
- Update `_load_chart_source_tracks` to return `Blended(primary=Charts, enrichments=[Local])` when local files merge
- Layer 5 (`source_truthfulness` block) ships here — now never cries wolf because `Blended` is the legitimate label
- Admin UI gains "Charts, blended with N of your tracks" — honesty as a feature

### **Apply the recipe to other hot spots** (1 PR each, can be queued):

1. `Segment.metadata["audio_source"]` → `StrEnum`, `Segment.__post_init__` validates MUSIC has non-empty `audio_source`
2. `AdPart.type` / `AdVoice.role` / `AdPart.role` → `StrEnum`s with normalization at scriptwriter boundary
3. `script.format` → validated against `AdFormat` enum at `_select_ad_creative` boundary

### **Future / nice-to-have:**

- `mutmut` nightly on `mammamiradio/playlist/`. Ships *after* the invariant test. Without the invariant test, mutmut score caps at 40% — not worth the noise.
- Track-level discrimination per industry pattern (Phase 5+).

## Confidence assessment after iteration 2

**Higher than after iteration 1.** Iteration 1 told us the root cause; iteration 2 told us the prescription was insufficient and the scope was too narrow. Five lenses converge again — different framings, complementary findings, no contradiction.

The plan is now actionable: tonight's CI fix is a 10-min PR; the operator-honesty PR is a 3-4 hour focused block; the follow-ups are self-contained. The "release BETTER not MORE" rule is preserved — each PR has a clean story, not a shopping list.

---

# Iteration 3 — external LLM (codex with web access + GitHub fetch)

**Trigger:** Florian: "still go do one /research --council execution on the theory. it has access to my github." Claude-in-Chrome MCP offline; ran one external pass via codex CLI with `--enable web_search_cached`. One external model, GitHub source fetch enabled, looked at actual commits `32d2529` + `352bf77` + `0ea2835`. Full transcript appended in `.context/research/2026-05-02-council-playlist-source-truthfulness.md`.

**Codex verdict (one line):** SHIP WITH MODIFICATIONS — the council's prescription is conceptually right but **must not collapse `PlaylistSource.kind` and `Track.source` into a single shared vocabulary**.

## Major correction — Layer A item 3 was wrong

The original Layer A item 3 said: *"Replace the `Track.source` / `PlaylistSource.kind` drift (`youtube` vs `charts`, no cross-mapping) with a single `Origin` enum. Both fields agree by construction, never by convention."*

**Codex pushback (verbatim):**
> "single Origin vocabulary unifying `PlaylistSource.kind` and `Track.source`" is too coarse. In current code, `Track.source` is a transport/acquisition origin (`youtube|jamendo|local|demo`), while `PlaylistSource.kind` is a playlist-level label (`charts|jamendo|local|demo|url`). `charts` is not a track origin. If you force one shared enum, you either lie on tracks or lose the concept of "charts" as a playlist source.

**Replace with two enums plus an explicit invariant map:**

```python
# mammamiradio/core/models.py
class TrackOrigin(StrEnum):       # transport / acquisition
    youtube = "youtube"
    jamendo = "jamendo"
    local = "local"
    demo = "demo"

class SourceKind(StrEnum):        # playlist-level label
    charts = "charts"             # → KIND_TO_TRACK_ORIGINS[charts] = {youtube} (or {youtube, local} once Blended ships)
    jamendo = "jamendo"           # → {jamendo}
    local = "local"               # → {local}
    demo = "demo"                 # → {demo}
    # NOTE: 'url' is NOT in this enum. See "url is not a domain variant" below.

KIND_TO_TRACK_ORIGINS: dict[SourceKind, set[TrackOrigin]] = {
    SourceKind.charts: {TrackOrigin.youtube},  # widen to {..., local} only if Blended is in this PR
    SourceKind.jamendo: {TrackOrigin.jamendo},
    SourceKind.local: {TrackOrigin.local},
    SourceKind.demo: {TrackOrigin.demo},
}

def assert_source_truth(source: PlaylistSource, tracks: Sequence[Track]) -> None:
    allowed = KIND_TO_TRACK_ORIGINS[SourceKind(source.kind)]
    actual = {TrackOrigin(t.source) for t in tracks}
    extra = actual - allowed
    if extra:
        raise ValueError(f"source.kind={source.kind!r} cannot contain track origins {extra}")
```

This guard runs **before `switch_playlist()` and before persistence**. The two enums + invariant map replace the unified-vocabulary recommendation everywhere it appears in Layer A and Layer B.

## Major correction — `kind="url"` is not a domain variant

`/api/playlist/load` currently accepts `kind="url"` and the resolver branches on it (`playlist.py:469`, `streamer.py:1952`). Codex caught that this is the wrong abstraction:

> "Treat `/api/playlist/load`'s current `kind=\"url\"` as an inbound command DTO/adaptor, not a persisted domain variant."

**Concrete:** define an `InboundSourceRequest` DTO at the FastAPI boundary that *can* carry `kind="url"`. The handler resolves `url` to a real `SourceKind` (e.g., `jamendo://...` → `SourceKind.jamendo`) before constructing the persisted/domain `PlaylistSource`. The persisted union never has a `url` member — that's an inbound concept only.

This kills a hidden lying surface: `kind="url"` today persists in `cache/playlist_source.json` and gets surfaced in `/status`, even though the URL has long since been resolved into actual jamendo/charts/local tracks.

## Stay on dataclasses, NOT Pydantic

Codex's pragmatic call (matches Lens 4 implementation-spec from iteration 2):

> "For a single-maintainer Python app already using dataclasses, I would not pull in Pydantic just for this. The 80/20 version is: `StrEnum`s for playlist kind and track origin, frozen dataclass variants or factory constructors for resolved sources, one shared `assert_source_truth(source, tracks)` guard before `switch_playlist()` and before persistence, plus the invariant tests and telemetry."

**Plan implication:** Layer A drops the Pydantic mention. The discriminated union becomes frozen dataclass variants with `__post_init__` calling `assert_source_truth`. Same safety, zero new dependencies, smaller delta.

## Blended — likely a property, not a 5th variant

Codex narrowed Layer C item 8 (`Resolution = Pure | Blended`):

> "I would not make `Blended` a heavyweight peer unless you expect many composition modes soon. For operators, the useful facts are: primary source, enrichments, and actual origin counts. So either `Pure(...) | Blended(primary, enrichments, tracks)` or a simpler `PlaylistSource(kind=\"charts\", enrichments=[\"local\"])` works. If the dashboard mostly needs 'what did you intend to load?' plus 'what is actually in here?', a decorator/property is enough."

**Plan revision:** prefer the lighter shape — `PlaylistSource` (or `ChartsSource`) gains `enrichments: tuple[str, ...] = ()`. `KIND_TO_TRACK_ORIGINS` widens dynamically based on `enrichments` (e.g., `charts` + `local` enrichment → `{youtube, local}` allowed). Promote to a separate `Blended` variant only if composition becomes first-class (e.g., charts+jamendo, jamendo+local, etc.).

This also resolves the Shift-2 sequencing block from iteration 2: telemetry can ship in the **same PR** as the discriminated union if `enrichments` is in scope, because the legitimate `charts+local` case computes `mismatch=False` automatically. Layer 5 and Layer 8 collapse into one PR — and `Blended` as a separate variant is deferred indefinitely (likely never needed).

## Codex on the deferral of per-track subtypes — correct call

> "Deferring per-track subtypes is the right prioritization. A `LocalTrack|YouTubeTrack|JamendoTrack` model family would catch field-shape errors early, but it would not by itself prevent `PlaylistSource(kind=\"charts\")` from containing only `LocalTrack`s. Your actual failure is a cross-object invariant, not a malformed single object."

This validates iteration 2's "Phase 5+" deferral. Per-track subtypes solve a *different* class of bug (field misuse) and don't cheaply close the cross-object invariant gap.

## Codex on root cause — semantic overloading, not just types

> "Highest-confidence root cause: semantic overloading at an architectural seam. `PlaylistSource.kind` is currently doing at least four jobs: inbound command (`kind=\"url\"` from `/api/playlist/load`), loader dispatch, persisted 'current source', and operator-facing truth label. That is the design failure. Plain `str` made it easier, but the real problem is that one field is standing in for intent, resolution path, and observed composition."

> "The process gap is real but secondary. `coverage-ratchet.py` does appear to let pytest failures slide if coverage parsing still succeeded… But it does not explain why the model allowed the bad state; it only explains why red evidence could be missed."

Both confirm iteration 1 + 2's framing. The CI swallow stays a non-negotiable parallel fix; the architectural fix is splitting the four jobs across types.

## Revised final sequencing (after iteration 3)

### **Tonight, ~10 min:** CI swallow fix — unchanged

`scripts/coverage-ratchet.py:72-74` two-line patch + dedicated `pytest tests/` step in `quality.yml` running before the coverage ratchet. Tiny PR.

### **Operator-honesty PR (resurrect #281):** the bundle

- **Two enums** (`SourceKind`, `TrackOrigin`) + `KIND_TO_TRACK_ORIGINS` invariant map (replaces "single `Origin` vocabulary")
- **Frozen dataclass variants** for resolved sources (no Pydantic)
- **`assert_source_truth(source, tracks)`** guard before `switch_playlist()` and before persistence
- **Parametrized invariant test** (Lens 3 paste-ready), registered for all loaders + `fetch_chart_refresh`
- **`Track.source` non-defaulted** in `models.py:49` (gap fix #3 from iteration 2)
- **`read_persisted_source` validates** against `SourceKind` enum; corrupt → log + delete (gap fix #5)
- **`InboundSourceRequest` DTO** at the FastAPI boundary; `kind="url"` lives only there, never persisted
- **`enrichments: tuple[str, ...] = ()`** field on `PlaylistSource`; `KIND_TO_TRACK_ORIGINS` widens dynamically
- **`source_truthfulness` telemetry** ships in this PR — `enrichments` makes legitimate `charts+local` honest (no false-positive crying wolf)

This is one focused PR (~140-180 LOC delta), no Pydantic dependency, no separate Blended-ADT follow-up needed.

### **Apply the recipe to other hot spots** — unchanged from iteration 2

`Segment.metadata["audio_source"]` (P0 next #280), `AdPart.type`, `AdVoice.role`, `script.format`, etc.

### **Mutmut nightly** — unchanged. Ships *after* the invariant test.

## Confidence assessment after iteration 3

**Higher still.** Three independent investigations (iteration 1 5-lens local, iteration 2 5-lens local + audit, iteration 3 external codex with GitHub access) converge on the same root cause and the same prescription shape. Codex's contributions are corrections within the prescription, not contradictions of it: don't unify vocabularies, don't add Pydantic, don't make `Blended` heavy, treat `url` as inbound DTO. All four corrections *simplify* the plan.

The plan is now ready to execute. CI swallow PR ships first; the operator-honesty bundle ships second on top of it. PR #281 stays in draft until both land.

No further iterations needed. Council confidence is very high.

---

# FINAL SHAPE — what we will actually do

*This section is the only one a future reader needs. The iteration trail above is preserved as forensics for why these decisions are trustworthy (three independent investigations converged). If you are about to execute, read only this section.*

## Two PRs, in order. Nothing else ships first.

### PR #1 — CI swallow fix (~10 min, ~10 LOC)

**Why first:** Without CI honesty, every other test added below is theatre. CI has been silently green with red tests since PR #279 (`scripts/coverage-ratchet.py:72-74` masks pytest non-zero exits when coverage parsing succeeds).

**Changes:**
- `scripts/coverage-ratchet.py`: hard-exit on `result.returncode != 0`, regardless of `modules` populated. Two-line patch.
- `.github/workflows/quality.yml`: add a dedicated `pytest tests/` step that runs **before** the coverage ratchet step. If pytest fails, coverage is irrelevant.

**Acceptance:** intentionally break a test on a throwaway branch; CI must go red.

### PR #2 — Operator-honesty bundle (~140–180 LOC, one focused session)

**Why bundled:** The invariant test, the discriminated union, and the truthfulness telemetry only work as a set. Splitting them either ships a checker without a thing to check, or ships a thing without a check.

**Changes (all in this single PR):**

1. **Two `StrEnum`s** in `mammamiradio/core/models.py`:
   - `TrackOrigin = {youtube, jamendo, local, demo}` (transport / acquisition)
   - `SourceKind = {charts, jamendo, local, demo}` (playlist-level label — `url` is *not* here)
2. **Invariant map** + guard in `mammamiradio/core/models.py`:
   ```python
   KIND_TO_TRACK_ORIGINS = {
       SourceKind.charts:  {TrackOrigin.youtube},
       SourceKind.jamendo: {TrackOrigin.jamendo},
       SourceKind.local:   {TrackOrigin.local},
       SourceKind.demo:    {TrackOrigin.demo},
   }
   def assert_source_truth(source, tracks): ...   # widens allowed set by source.enrichments
   ```
3. **`enrichments: tuple[str, ...] = ()`** on `PlaylistSource` — represents "charts blended with local files" honestly without a separate `Blended` variant.
4. **`Track.source` non-defaulted** in `models.py:49` — missing → raise. Eliminates the `youtube` default that hid drift.
5. **Frozen dataclass variants** for resolved sources (`ChartsSource | JamendoSource | LocalSource | DemoSource`) with `__post_init__` calling `assert_source_truth`. **No Pydantic added.**
6. **`assert_source_truth(source, tracks)` is called at exactly two seams:** before `switch_playlist()`, and before `_persist_source(...)`. Anywhere else is optional.
7. **`InboundSourceRequest` DTO** at the `/api/playlist/load` FastAPI boundary. `kind="url"` lives only on the inbound DTO; the handler resolves the URL into a real `SourceKind` before constructing/persisting `PlaylistSource`. `cache/playlist_source.json` never holds `kind="url"` again.
8. **`read_persisted_source` validates** parsed `kind` against `SourceKind`. Corrupt → log + delete the cache file; do not refuse to boot.
9. **Parametrized invariant test** in `tests/playlist/test_source_kind_invariant.py`. Registers all loaders (`charts`, `jamendo`, `local`, `demo`, plus `fetch_chart_refresh`). Asserts every track's `source` ∈ `KIND_TO_TRACK_ORIGINS[result.kind] ∪ enrichments`. Adding a future loader requires adding one tuple — that's the whole onboarding cost.
10. **`source_truthfulness` block** in `_public_status_payload`:
    ```python
    "source_truthfulness": {
        "declared_kind": source.kind,
        "enrichments": list(source.enrichments),
        "track_origins": dict(Counter(t.source for t in state.playlist)),
        "mismatch": <result of assert_source_truth, as bool>,
    }
    ```
    Surfaced in admin Engine Room. The legitimate `charts + local enrichment` case computes `mismatch=False` (because `local` is in `enrichments`), so telemetry never cries wolf.

**Acceptance criteria — all four must hold:**

- [ ] PR #1 has merged. Pytest failures fail CI.
- [ ] Parametrized invariant test green; would fail if any of `32d2529` / `352bf77` were re-introduced.
- [ ] `/public-status` and admin Engine Room show `source_truthfulness` with `mismatch=false` for each of: `(allow_ytdlp=False, jamendo=missing, music/=empty)`, `(allow_ytdlp=True, charts API returning tracks, music/=present, enrichments=["local"])`, `(allow_ytdlp=False, music/=present)`, `(jamendo configured, returns empty)`.
- [ ] `cache/playlist_source.json` from a fresh `/api/playlist/load { kind: "url", value: "jamendo://..." }` request contains `kind: "jamendo"`, never `kind: "url"`.

## Explicitly out of scope for this work

These have been considered and intentionally deferred. Each ships **only if it bites again**, not as part of this work:

- **Per-track discriminated subtypes** (`LocalTrack | YouTubeTrack | JamendoTrack`). Cascades into ~30 files. Doesn't close the cross-object invariant gap. Codex confirmed deferral is correct.
- **Resolution = Pure | Blended ADT.** Replaced by the cheaper `enrichments` tuple. Promote to a real variant only if composition becomes first-class (charts+jamendo, etc.).
- **Recipe applied to other hot spots** (`Segment.metadata["audio_source"]`, `AdPart.type`, `AdVoice.role`, `script.format`, `HostPersonality.engine`). Each is a separate small PR after PR #2 has soaked. Order by user-visible severity (`audio_source` first).
- **Mutmut nightly.** Ships after the invariant test has soaked one release. Without the invariant test, mutation score caps at ~40%.
- **Per-loader self-labeling refactor / killing `_load_chart_source_tracks`'s in-place mutation.** The frozen variants in PR #2 already make caller-side relabeling structurally impossible because tracks are passed at construction; the further refactor is cosmetic.

## What "done" looks like

After PR #2 merges:
- Every `PlaylistSource` instance in memory has been validated at construction against its tracks. Mismatched construction is a `ValueError` at the boundary.
- The `/public-status` payload tells the truth on every poll, including the legitimate `charts + local` blend case.
- Adding a new music source is one new variant + one new entry in `KIND_TO_TRACK_ORIGINS` + one tuple in the invariant test. The bug class is structurally closed for this hot spot.

PR #281 in its current form is **abandoned** — its fixes are subsumed by PR #2's frozen-variant + `assert_source_truth` enforcement. Close #281 with a comment pointing at PR #2.
