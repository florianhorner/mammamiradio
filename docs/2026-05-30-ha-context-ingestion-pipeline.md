# HA Context Ingestion Pipeline

**Status:** Draft design — 2026-05-30
**Author:** Florian + Claude (Opus 4.7)
**Supersedes:** the hardcoded allowlist in `mammamiradio/home/ha_context.py`

## 1. Problem

`ha_context.py` works beautifully — *in one apartment*. The four hand-curated dicts (`GOLD/SILVER/BRONZE_ENTITIES`, `ENTITY_LABELS[_EN]`, `STATE_TRANSLATIONS[_EN]`, `REACTIVE_TRIGGERS`) hardcode Florian's specific Zigbee/Z-Wave/Shelly inventory in Italian and English. The mood classifier is a 40-line `if`-ladder over those same entity IDs.

Consequences:

- **Customer ceiling.** Any HA install that isn't Florian's PentFLOuse gets *zero* radio personality from HA — the addon ships looking dead. The Aqara FP300 question made this concrete: even *Florian's own new sensor* needs four dict edits before the host can mention it.
- **Maintenance ceiling.** Every new device = touch four dicts + maybe a mood rule + maybe a trigger. This is hostile to experimentation.
- **Intelligence in the wrong layer.** Brittle `if`-statements do work an LLM does better.

## 2. Goals & non-goals

**Goals**

- Any HA install (zero customer code) yields ≥3 contextually-grounded banter moments per hour.
- New devices (Aqara FP300, vibration sensors, anything) appear in radio narration with **no code change** within one polling cycle of pairing.
- Token cost per banter generation is **bounded regardless of home size** (10 vs 2,000 entities).
- The illusion (Principle #1) is *strictly improved*: no telemetry-reading, no robotic enumeration.
- Operator can see and override what's being sent to the LLM from the Engine Room.

**Non-goals**

- Real-time WebSocket streaming. REST polling stays; events are derived from successive snapshots, as today.
- Replacing the audio pipeline, mood model, or scriptwriter prompt structure. This is purely about the *input* layer feeding `scriptwriter.py`.
- Multi-user / multi-home (one HA per addon stays the assumption).
- Localization beyond Italian + English in this round.

## 3. Decisions (this doc)

- **D1: Hybrid curation.** LLM auto-generates labels for any home, hand-tuned dicts remain as an *override* layer for signature entities.
- **D2: Conservative default-deny privacy.** Sensitive categories (location, cameras, alarms, raw GPS, person-tracking) never reach the LLM prompt without explicit operator opt-in via addon config / `radio.toml`.

## 4. Architecture — five layers

```
HA REST /api/states + registries
        │
        ▼
[L1 Ingest]    fetch all entities + entity/device/area registries
        │
        ▼
[L2 Filter]    denylist by domain / device_class / entity_category / privacy
        │
        ▼
[L3 Score]     rank each survivor for "radio interestingness"
        │
        ▼
[L4 Budget]    top-N by score + all recent events  →  hard token ceiling
        │
        ▼
[L5 Narrate]   hand-tuned overrides + LLM-generated label cache  →  prompt text
        │
        ▼
scriptwriter.py (unchanged interface: HomeContext.summary / .events_summary / .mood / .weather_arc)
```

### L1 — Ingest

- Add a **registry fetch** alongside `/api/states`: entity registry, device registry, area registry (REST or one-shot WebSocket). Cache for the addon lifetime, refresh on schedule or on first-seen entity.
- Areas (`area.kitchen`, `area.bedroom`) become first-class context. The FP300 paired in `area.bedroom` automatically inherits "bedroom" semantics — that's where the sleep-persona magic comes from with zero per-device code.
- Persist registry snapshot to `cache/ha_registry.json` so a cold start has it before HA replies.

### L2 — Filter (denylist, not allowlist)

Default-deny rules — generalize to every home:

| Category | Action | Reason |
|---|---|---|
| `domain ∈ {update, button, scene, automation, script, zone}` | drop | Not state-of-the-world |
| `entity_category ∈ {diagnostic, config}` | drop | Telemetry |
| `device_class ∈ {signal_strength, battery, timestamp}` | drop | Telemetry |
| `state ∈ {unknown, unavailable}` | drop | Already done today |
| Entity in **privacy_deny_default** set (D2): `device_tracker.*`, `person.*` GPS attributes, `camera.*`, `alarm_control_panel.*` | drop unless opt-in | D2 |
| `friendly_name` or state matches injection patterns | sanitize | Already done today via `_sanitize_state_value`; widened |

Privacy opt-in lives in addon config:

```yaml
ha_privacy:
  share_person_presence: true   # home/away only, never GPS
  share_cameras: false
  share_alarm: false
  share_device_trackers: false
```

`person.*` keeps the home/away state by default; GPS attributes are stripped before scoring.

### L3 — Score "radio interestingness"

Each survivor gets a score from a small set of additive signals:

- **Recency-of-change**: time since `last_changed`. Just-flipped > steady-for-days.
- **Domain salience weights** (tunable):
  `media_player: 1.0, vacuum: 0.9, presence/occupancy: 0.9, lock: 0.8, weather: 0.8, climate: 0.7, light: 0.6, fan: 0.6, switch: 0.5, sensor[power]: 0.5, sensor[*]: 0.2`
- **Area resolution**: entity has an area → +0.2 (the host can localize).
- **State change vs steady**: bonus for entities in the events buffer.
- **Override boost**: any entity present in the hand-tuned `ENTITY_LABELS` dict gets +0.5 — your craft still wins the ranking.

Scoring is a pure function; testable in isolation with synthetic entity dicts.

### L4 — Budget

Two hard ceilings, enforced before narration:

- **Top-N entities by score** (default `N=12`, configurable). Steady state.
- **All recent events** within the existing 30-min window (already bounded to 20 by `EVENT_BUFFER_SIZE`).
- **Total prompt-section character cap** (default `2000`): truncate lowest-scored entities first.

This is the answer to "why not capture all?" — we *capture* all, but only the top slice is *sent*. Token cost is decoupled from home size.

### L5 — Narrate (hybrid per D1)

Two narration sources, override-then-fallback:

1. **Hand-tuned dicts** (today's `ENTITY_LABELS[_EN]`, `STATE_TRANSLATIONS[_EN]`) — unchanged. Signature entities keep their voice.
2. **LLM-generated label catalog** — for any entity *not* in the dicts:
   - Periodic background pass (e.g. on first-seen, then daily) calls Claude/OpenAI with: entity_id, domain, area, friendly_name, sample states, unit.
   - Returns: `{italian_label, english_label, state_translations: {...}}`.
   - Cached to `cache/ha_label_catalog.json`, keyed by entity_id + `attributes_hash`.
   - Invalidated when `friendly_name` or `area` changes.
   - Generation cost is amortized to ~free: a 50-entity catalog is one prompt, refreshed daily.

Hand-tuned dicts beat catalog entries on lookup. Catalog entries beat raw entity IDs. Raw entity IDs never reach the host (anti-illusion guard).

The hardcoded **mood classifier** (`classify_home_mood`) stays in Phase A as a fast-path heuristic; Phase C is the conversation about whether to retire it for an LLM scene-namer over the budgeted set.

## 5. Privacy model (D2)

- Default-deny set lives in code, not config — operator can *widen* via config, never silently expanded by a release.
- Admin Engine Room gets a new panel: **"What HA sees the LLM"** — renders the L4-budgeted slice in real time, with a copy button. No hidden state.
- `_sanitize_state_value` extended to scrub: lat/long-shaped strings, email addresses, IP addresses, MACs, anything matching `^[A-Z0-9_]{16,}$` (looks like a token).
- Same prompt-injection patterns as today, expanded denylist.
- New invariant test: privacy-denied entities never appear in `HomeContext.summary` or `.events_summary`.

## 6. Data model

Minimal additions to `HomeContext`:

```python
@dataclass
class ScoredEntity:
    entity_id: str
    area: str | None
    domain: str
    score: float
    raw_state: dict
    label_it: str
    label_en: str

@dataclass
class HomeContext:
    # existing fields untouched
    ...
    # new (additive)
    scored: list[ScoredEntity] = field(default_factory=list)
    catalog_hit_rate: float = 0.0  # observability: % of summary lines from catalog vs override
```

The scriptwriter consumes `summary` / `events_summary` / `mood` / `weather_arc` the same way it does today. **No scriptwriter prompt changes in Phase A.**

## 7. Phasing

### Phase A — Generalization, behavior-preserving (1 PR)

- L1 registries + L2 denylist + L3 scoring + L4 budgeting.
- Hand-tuned dicts remain authoritative; catalog not built yet.
- On Florian's home: output is byte-for-byte close to today's (same entities top the score because of the override boost).
- On any other home: HA goes from silent to "interesting things in your home, identified by domain + area."
- **Visible deliverable:** Engine Room panel showing scored set + denylist hits.

### Phase B — LLM label catalog (1 PR)

- Background catalog generator + disk cache + invalidation.
- New homes now get *narrated* (Italian + English), not just enumerated.
- Hand-tuned dicts still authoritative on Florian's signature entities.
- **Visible deliverable:** "catalog hit rate" observability + manual "regenerate catalog" button in Engine Room.

### Phase C — Mood + scenes by LLM (1 PR, decision point)

- Replace `classify_home_mood` priority ladder with an LLM scene-namer over the L4-budgeted set, cached for `mood_ttl_seconds` (e.g. 90s) to keep cost bounded.
- Keep the hardcoded ladder as a *fallback* when LLM is unavailable, not as the primary path.
- **Open question:** is the LLM scene-namer worth the latency / cost vs. a smarter heuristic? Re-decide at Phase C kickoff with Phase B data in hand.

Each phase ships independently. Each phase's PR follows scope discipline (no planning-doc hitchhikers).

## 8. Test plan — audio-delivery three-scenario rule

Per `CLAUDE.md` audio-delivery rule, every phase covers all three:

| Scenario | Test |
|---|---|
| **Normal** | Synthetic 50-entity HA snapshot → asserts top-N selection, scoring order, denylist hits, narrated lines. |
| **Empty** | HA returns 0 entities or all entities filtered out → `HomeContext.summary == ""`, scriptwriter falls through to its existing no-HA path. Stream continues. |
| **Post-restart** | Cold start with no `cache/ha_registry.json`, no label catalog, HA reachable: first poll degrades to domain-only labels (no Italian flair yet), still produces banter. Second poll, catalog warm: full narration. |

Plus phase-specific tests:

- **Privacy invariant** (Phase A): seed snapshot includes `device_tracker.foo` with GPS attrs → asserts entity never appears in any output field.
- **Override precedence** (Phase A/B): same entity in dicts and catalog → dict wins.
- **Catalog cache invalidation** (Phase B): `friendly_name` change → catalog entry regenerates.

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Token budget for catalog generation drifts up | Cache to disk, regenerate on schedule only, hard ceiling on entity count per generation call |
| LLM-generated labels are *worse* than entity_id passthrough | Quality gate: catalog gen rejects responses that look like JSON garbage or echo the entity_id verbatim. Fallback to "friendly_name + state" if rejection |
| Privacy leak via attributes (not just state) | L2 strips known sensitive attribute keys (`latitude`, `longitude`, `gps_accuracy`, `source_type`) globally before L3 sees them |
| Score function tuned for Florian's home only | Score weights live in config (`radio.toml` or addon options), not constants. Document the tuning knob |
| Customer addon starts narrating sensitive household state by surprise | Engine Room "What HA sees the LLM" panel is the canary. First-run experience surfaces it |
| Hardcoded mood classifier and LLM scene-namer disagree on Florian's home | Phase C decision: data-driven. If LLM consistently produces less interesting moods, keep the ladder |

## 10. Doc-sync touchpoints

(per the **Doc sync** rule in `CLAUDE.md` — same-commit updates):

- `docs/architecture.md` — new HA ingestion layer section
- `docs/operations.md` — privacy defaults + how to opt in
- `CLAUDE.md` — new env vars (`MAMMAMIRADIO_HA_PRIVACY_*`, `MAMMAMIRADIO_HA_BUDGET_*`)
- `CHANGELOG.md` — public-facing summary per phase (editorial-boundary clean)
- `docs/runbooks/ha-addon.md` — addon options schema additions

## 11. Open questions

1. Should privacy opt-ins also be **per-entity** (admin UI checkbox list), not just per-category? Probably yes for v2; per-category for v1.
2. Phase C: LLM scene-namer or smarter heuristic? Re-decide post Phase B.
3. Multi-resident dynamic (`person.*` count > 2) is implicit in the scoring layer — do we need explicit "guest detected" / "solo" / "couple" scene classes, or does the LLM handle it free-form? Probably free-form, but worth measuring.
4. Vibration sensors specifically: these are *events without state* (a tap doesn't have a "state" worth ranking). Phase A's event pipeline already handles this — verify with a synthetic Aqara vibration event in Phase A's test snapshot.

---

**Bottom line:** Phase A is the unlock. It's the smallest change that makes the addon valuable to anyone-but-you, and it's the foundation Phases B and C build on. The FP300 and vibration sensors are not features to add — they're *the test case* for whether Phase A works.
