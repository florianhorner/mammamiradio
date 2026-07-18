# Festival Mode

Festival Mode turns your AI hosts into theatrical music-competition MCs. Every song becomes a delegation from a fictional Italian region, judges award dramatic scores, and the drinking-game triggers are mandatory.

Turn it on for music-competition watch parties, Sanremo screenings, or any gathering where "theatrically unhinged Italian commentary" is the vibe.

## Enabling Festival Mode

### From the admin panel (recommended)

1. Open `/admin` and scroll to the **Speciali** section.
2. Click the **★ Festival Mode** toggle.
3. The station reserves fresh protected runway when available, replaces the
   lookahead queue, and schedules the next generated festival-flavored banter.
   Current audio and any preserved runway continue normally.

No restart required. The toggle persists across server restarts.

### Via environment variable

Set `MAMMAMIRADIO_FESTIVAL_MODE=true` before starting the server:

```bash
MAMMAMIRADIO_FESTIVAL_MODE=true python -m uvicorn mammamiradio.main:app
```

The env var overrides the admin toggle. To disable at runtime, clear the env var and use the admin API.

### Via Home Assistant add-on

In your HA add-on configuration:

```yaml
festival_mode: true
```

The add-on maps this to `MAMMAMIRADIO_FESTIVAL_MODE` at container startup.

## API reference

### GET /api/party

Returns the current party mode state. Requires admin auth.

```
GET /api/party
```

**Response:**

```json
{"active": true, "mode": "festival"}
```

| Field | Type | Description |
|---|---|---|
| `active` | bool | `true` if any party mode is active |
| `mode` | `"festival"` \| `null` | Active mode name, or `null` when off |

### POST /api/party

Toggle Festival Mode. Requires admin auth. Idempotent — calling enable twice returns ok without side-effects.

```
POST /api/party
Content-Type: application/json

{"action": "enable", "mode": "festival"}
```

```
POST /api/party
Content-Type: application/json

{"action": "disable"}
```

| Field | Type | Required | Values |
|---|---|---|---|
| `action` | string | yes | `"enable"` or `"disable"` |
| `mode` | string | when enabling | `"festival"` (only valid value) |

**Response:**

```json
{"ok": true, "active": true, "mode": "festival"}
```

**Error:** `422` if `action` is invalid or `mode` is not `"festival"` when enabling.

## What happens when you enable Festival Mode

1. The control reserves protected continuity audio and discards pre-produced
   normal-mode segments when safe replacement audio is available. If the
   fallback has no fresh runway, the existing playable head/slot is preserved so
   current audio is not cut into a gap.
2. `state.force_next` is set to `BANTER` so the first new segment is festival-flavored commentary.
3. The LLM prompt for all subsequent banter segments receives the `FESTIVAL_MODE_BLOCK` injection (defined in `mammamiradio/hosts/prompt_world.py`, injected by `write_banter` in `scriptwriter.py`), which instructs hosts to:
   - Announce each song as a fictional Italian-regional delegation
   - Award dramatic point scores per track
   - Call at least one drinking-game trigger per song intro

## Drinking game triggers

Hosts are prompted to call at least one of these per song introduction:

| Trigger phrase | Audience response | Moment |
|---|---|---|
| `CHIAVE MUSICALE!` | tutti! | Key change detected |
| `WIND MACHINE ATTIVATA!` | bevi! | Wind machine moment |
| `NOTA LUNGA!` | drink — hold it — hold it — NOW! | Sustained note |
| `BALLERINI INUTILI!` | un sorso | Unnecessary backing dancers |
| `CAMBIO DI TONALITÀ!` | drink in solidarity | Dramatic modulation |

For non-song banter (listener requests, station IDs), hosts keep the theatrical festival energy but drop the delegation framing and point scoring — those belong to song introductions only.

## What happens when you disable Festival Mode

The config flag clears and the env var is updated. Unlike enabling, **the queue is not purged** — any in-flight festival segment plays to completion so the stream does not drop. Normal host behavior resumes after the current segment finishes.

## Persistence

| Deployment | Storage |
|---|---|
| Standalone / Docker | `MAMMAMIRADIO_FESTIVAL_MODE` key in `.env` |
| Home Assistant add-on | `festival_mode` field in `/data/options.json` |

The state survives server restarts. On boot, `config.py` reads the env var and sets `config.party_mode = "festival"` if it is truthy.

## Verification

```bash
# Check current state
curl -s -u admin:YOUR_PASSWORD http://localhost:8000/api/party

# Enable Festival Mode
curl -s -X POST http://localhost:8000/api/party \
  -H "Content-Type: application/json" \
  -u admin:YOUR_PASSWORD \
  -d '{"action":"enable","mode":"festival"}'

# Confirm active
curl -s -u admin:YOUR_PASSWORD http://localhost:8000/api/party
# → {"active":true,"mode":"festival"}

# Disable
curl -s -X POST http://localhost:8000/api/party \
  -H "Content-Type: application/json" \
  -u admin:YOUR_PASSWORD \
  -d '{"action":"disable"}'
```

## Legal constraint

The prompt block explicitly forbids the terms "Eurovision", "ESC", and "EBU". Do not add them to the host configuration, `FESTIVAL_MODE_BLOCK`, or any UI copy. The fictional-delegation framing avoids any real competition brand while preserving the comedic format.

## Related

- [Adding a new party mode theme](party-mode-extension.md) — how to implement a second theme (e.g. Hitster, World Cup)
- [Architecture → `/api/party` route](architecture.md) — route table entry for the toggle endpoint
- [Chaos Mode](architecture.md#segment-production) — similar first-strike + idempotent toggle pattern
