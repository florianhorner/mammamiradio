# Adding a New Party Mode Theme

## Why the party_mode slot exists

Celebration-night themes need theatrical behavior that normal stations don't — scoring, drinking rules, over-the-top commentary. But they also need to be:

- **Toggleable at runtime** without a server restart
- **Idempotent** so double-enabling does not cause double queue purges
- **Persistent** so a restart at 11 PM doesn't kill the party vibe
- **Safely isolated** from the chaos mode system, which has a different first-strike mechanism

Rather than making each theme a one-off patch, Festival Mode landed as the first concrete theme inside a `party_mode` slot on `StationConfig`. The slot's type is a union literal:

```python
PartyMode = Literal["festival"]
```

Adding `"hitster"` means widening that literal and adding a prompt block — everything else (API, UI toggle, persistence, idempotency) is already wired.

## Design decisions

### party_mode lives on StationConfig, not StationState

`StationState` is the in-memory, session-scoped runtime state. It resets between restarts. `StationConfig` is the fully resolved, env-sourced config that persists. Party mode must survive HA watchdog restarts, so it belongs on `StationConfig`.

`super_italian_mode` follows the same pattern — both are operator behavior flags, not session metrics.

### Queue purge on enable only

Enabling purges the lookahead queue and arms `state.force_next = SegmentType.BANTER`. This gives listeners an immediate first-strike festival segment instead of hearing the last pre-produced normal banter play out.

Disabling does **not** purge. Purging on disable would drop any in-flight festival segment and cause dead air between the toggle and the next produced normal segment. The in-flight segment plays to completion; normal behavior resumes naturally.

### Separate _party_lock

The party toggle uses its own `asyncio.Lock` (`_party_lock`) rather than sharing the `source_switch_lock` used by playlist changes. This keeps the lock surface small — party toggles don't need to serialize against source-change operations.

## How to add a new theme

### 1. Widen the type alias

In `mammamiradio/core/models.py`:

```python
# Before
PartyMode = Literal["festival"]

# After
PartyMode = Literal["festival", "hitster"]
```

### 2. Write the prompt block

In `mammamiradio/hosts/scriptwriter.py`, add a constant after `FESTIVAL_MODE_BLOCK`:

```python
HITSTER_MODE_BLOCK = """\
HITSTER MODE — MUSIC QUIZ HOST:
[Your instructions here...]
Never reference any real licensed game brand.\
"""
```

### 3. Inject the block into the banter prompt

Find the two lines near the `write_banter` call that build `festival_block`:

```python
festival_block = f"\n\n{FESTIVAL_MODE_BLOCK}" if config.party_mode == "festival" else ""
```

Replace with a mapping:

```python
_PARTY_BLOCKS = {
    "festival": FESTIVAL_MODE_BLOCK,
    "hitster": HITSTER_MODE_BLOCK,
}
party_block = f"\n\n{_PARTY_BLOCKS[config.party_mode]}" if config.party_mode else ""
```

Update the f-string below to use `party_block` instead of `festival_block`.

### 4. Add API validation

In `mammamiradio/web/streamer.py`, update the mode check in `set_party`:

```python
# Before
if action == "enable" and mode != "festival":
    return JSONResponse(..., status_code=422)

# After
_VALID_MODES: set[PartyMode] = {"festival", "hitster"}
if action == "enable" and mode not in _VALID_MODES:
    return JSONResponse(..., status_code=422)
```

### 5. Add the HA add-on option

In `ha-addon/mammamiradio/config.yaml`:

```yaml
options:
  hitster_mode: false

schema:
  hitster_mode: bool?
```

In `ha-addon/mammamiradio/translations/en.yaml`:

```yaml
configuration:
  hitster_mode:
    name: Hitster Mode
    description: "Turns your station into a music quiz host — one-shot questions, point scoring, time pressure."
```

In `ha-addon/mammamiradio/rootfs/run.sh`, add after the festival block:

```python
hitster = opts.get('hitster_mode', False)
hitster_val = 'true' if hitster else 'false'
print('export MAMMAMIRADIO_HITSTER_MODE=' + hitster_val)
```

### 6. Add env var loading in config.py

In `mammamiradio/core/config.py`, mirror the `MAMMAMIRADIO_FESTIVAL_MODE` block:

```python
_hitster_env = os.getenv("MAMMAMIRADIO_HITSTER_MODE", "").strip().lower()
if _hitster_env in _TRUTHY:
    config.party_mode = "hitster"
elif _hitster_env in _FALSY:
    config.party_mode = None
```

Place after the festival env block. Note: the last truthy env var wins — design for mutual exclusion at the operator level, not in code.

### 7. Add admin UI toggle

In `mammamiradio/web/templates/admin.html`, copy the `festivalControl` div and update IDs and labels. Mirror `loadFestivalToggle()` and `toggleFestivalMode()` with hitster equivalents. Use the same `--sun`/`--sun2` golden color theme for consistency.

### 8. Write the tests

Three mandatory scenarios from the [audio delivery test coverage rule](../CLAUDE.md):

1. **Normal**: enable → prompt includes hitster block, queue purged, first segment is BANTER; disable → mode cleared, no purge
2. **LLM down**: mode arms (`config.party_mode = "hitster"`), but script generation falls back gracefully
3. **Post-restart**: `MAMMAMIRADIO_HITSTER_MODE=true` in env → config loads with `party_mode = "hitster"` after cold boot

Add idempotence, auth, and stacking (hitster + chaos simultaneously) tests. See `tests/web/test_festival_mode.py` for the full pattern.

### 9. Update docs

- Add the new theme to `docs/festival-mode.md` or create a dedicated `docs/hitster-mode.md`
- Add the env var to `CLAUDE.md` Environment section
- Add the route to `docs/architecture.md` if any new routes are added
- Add a CHANGELOG entry

## Files touched by any party mode theme

| File | What changes |
|---|---|
| `mammamiradio/core/models.py` | Widen `PartyMode` literal |
| `mammamiradio/hosts/scriptwriter.py` | New `*_MODE_BLOCK` constant + injection |
| `mammamiradio/web/streamer.py` | Mode validation in `set_party` |
| `mammamiradio/core/config.py` | Env var reader |
| `mammamiradio/web/templates/admin.html` | UI toggle |
| `ha-addon/mammamiradio/config.yaml` | Addon option |
| `ha-addon/mammamiradio/translations/en.yaml` | Option label |
| `ha-addon/mammamiradio/rootfs/run.sh` | Env var mapping |
| `CLAUDE.md` | Environment section |
| `docs/architecture.md` | Route table (if new routes) |
| `CHANGELOG.md` | Release note |
| `tests/web/test_<theme>_mode.py` | 3 mandatory scenarios + coverage |

## Related

- [Festival Mode](festival-mode.md) — the first implemented party mode theme
- [Architecture → Chaos Mode](architecture.md#segment-production) — similar idempotent toggle pattern (different state placement)
