# `/api/integrations/v1/now-playing`

Stable read-only JSON contract that exposes mammamiradio's current stream
state to external music controllers (Music Assistant providers, custom
Home Assistant cards, future Sonos/Plex/community integrations).

## Hello world

```bash
curl http://homeassistant.local:8000/api/integrations/v1/now-playing | jq
```

Three lines of code render the live track in any HA card:

```javascript
const r = await fetch('/api/integrations/v1/now-playing');
const { now_playing } = await r.json();
if (now_playing?.segment_class === 'music') showTrackCard(now_playing.title, now_playing.artist, now_playing.artwork);
```

## Response shape

The endpoint always returns `200 OK` with a stable payload. Degradations
(stopped session, empty queue, unknown segment) are expressed in the
payload's `session_state` and `segment_class` fields — not as HTTP errors.

```json
{
  "schema_version": "1",
  "station": {
    "name": "Mamma Mi Radio",
    "frequency": "94.7",
    "theme": "Italia da bere",
    "hosts": [
      { "engine_host": "gianni", "display_name": "Gianni", "description": "..." }
    ]
  },
  "stream": {
    "relative_url": "/stream",
    "absolute_url": "http://homeassistant.local:8000/stream",
    "audio_format": {
      "codec": "mp3",
      "mime_type": "audio/mpeg",
      "bitrate_kbps": 192,
      "sample_rate_hz": 44100,
      "channels": 2
    }
  },
  "now_playing": {
    "segment_class": "music",
    "segment_type": "music",
    "title": "Volare",
    "started_at": 1746500000.0,
    "duration_estimate_sec": 210.0,
    "artist": "Domenico Modugno",
    "artwork": "https://example.test/art.jpg",
    "album": "Mr Volare",
    "year": 1958,
    "external_ids": { "spotify": "v01", "youtube": "y01" },
    "host": null,
    "context": {}
  },
  "up_next": [
    { "segment_class": "music",  "segment_type": "music",  "title": "Sapore di Sale",  "predicted": false },
    { "segment_class": "voice",  "segment_type": "banter", "title": "Host banter",     "predicted": true  }
  ],
  "session_state": "live",
  "changed_at": 1746500000.0
}
```

### `segment_class` — display bucket (USE THIS)

The stable bucket your UI branches on. Three classes, plus a sentinel:

| `segment_class` | Meaning | Use case |
| --- | --- | --- |
| `music` | A track. `artist`, `artwork`, `external_ids`, `album`, `year` populated when known. | Render a music card. |
| `voice` | A host segment (banter, news flash). `host` populated. | Render a host card with the host's name. |
| `interstitial` | Ad, station ID, time check, sweeper. Title-only. | Render a station card (no track shape). |
| `unavailable` | Transient: skipping, unknown future segment type. | Render a generic "on air" state without trying to populate music fields. |

### `segment_type` — raw internal subtype (diagnostic)

The raw `SegmentType.value` from mammamiradio's internal enum
(`music`, `banter`, `ad`, `news_flash`, `station_id`, `time_check`,
`sweeper`). Carry it through if you log or debug; do NOT branch on it for
core UX — new values may appear within v1 (additive-only).

### `session_state`

| Value | Meaning |
| --- | --- |
| `live` | Actively playing. `now_playing` is non-null. |
| `stopped` | Admin pressed stop OR a restart restored a persisted stop. `now_playing` is `null`. Show "off air" — do NOT show a stale track. |
| `empty_queue` | Boot or post-restart before the queue warms. `now_playing` is `null`. Show "queuing up" or "loading". |

### `stream.relative_url` vs `stream.absolute_url`

- **`relative_url`** is canonical and always present (`/stream`).
- **`absolute_url`** is opt-in. It is present when computable from the
  request URL AND the request is not behind Home Assistant Supervisor
  ingress (detected via `X-Ingress-Path`). Under ingress, `absolute_url` is
  omitted so consumers don't bake a per-session token into config.
- **Recommended:** if you're running on the same Home Assistant instance,
  always resolve `relative_url` against your known mammamiradio addon URL.
  If you're a remote/external consumer (Sonos PoC, public-internet poller),
  use `absolute_url` when present.

### `changed_at`

Floating-point epoch timestamp of the most recent observable state
mutation. Updated when:

1. The current segment changes (a new `Segment` starts playing).
2. The session stop flag flips (`/api/stop` or `/api/resume`).

The `changed_at` value is folded into the weak ETag (see below) so
consumers using `If-None-Match` will only revalidate when state actually
moves.

## Caching: ETag + Cache-Control

Every response carries:

```
ETag: W/"<weak fingerprint>"
Cache-Control: public, max-age=2
```

Subsequent requests should send `If-None-Match` to get a `304 Not
Modified` when nothing has changed. Use `HEAD` (or `GET` with a discarded
body) to fetch the ETag without paying for the JSON:

```bash
# HEAD returns the ETag + Cache-Control headers, no body.
ETAG=$(curl -sI -X HEAD http://host/api/integrations/v1/now-playing | grep -i ETag | awk '{print $2}' | tr -d '\r')
curl -i -H "If-None-Match: $ETAG" http://host/api/integrations/v1/now-playing
# HTTP/1.1 304 Not Modified
```

The ETag is a BLAKE2b digest of the serialized JSON body, so any
visible change to the payload invalidates the cache — including the
``up_next`` contents flipping under ``force_next`` even when the queue
length stays the same.

## Error contract

Known degradations stay in the payload:

| Situation | HTTP | Payload signal |
| --- | --- | --- |
| Queue empty / station booting | `200` | `session_state: "empty_queue"`, `now_playing: null` |
| Admin pressed stop | `200` | `session_state: "stopped"`, `now_playing: null` |
| Future SegmentType you don't recognise | `200` | `now_playing.segment_class: "unavailable"` |

True HTTP errors (route missing, server crash, future v1.1 schema
mismatch) return RFC 7807 `application/problem+json` bodies with `code`,
`message`, `cause`, `fix`, and `docs_url`. **No payload-level "error"
field exists** — if you get a `200`, the body is the contract.

## Security

- **No PII.** The endpoint exposes only fields a listener can already
  observe via the public stream.
- **No internal metadata.** Internal segment fields (signed URLs, file
  paths, download error strings, source-kind hints) are filtered through
  a server-side allowlist before serialization. Adding a new metadata key
  to mammamiradio does not automatically surface it here.
- **Read-only.** No mutating operations live under
  `/api/integrations/v1/`. Admin endpoints stay under `/api/*` behind
  `ADMIN_TOKEN` / `ADMIN_PASSWORD`.

## Multi-language quickstart

### curl

```bash
curl -s http://host:8000/api/integrations/v1/now-playing | jq '.now_playing | {title, artist, segment_class}'
```

### Python (`requests`)

```python
import requests
r = requests.get("http://host:8000/api/integrations/v1/now-playing", timeout=5)
r.raise_for_status()
data = r.json()
np = data["now_playing"]
if np and np["segment_class"] == "music":
    print(f"{np['title']} — {np['artist']}")
```

### JavaScript (`fetch`)

```js
const r = await fetch('/api/integrations/v1/now-playing');
const { now_playing, session_state } = await r.json();
if (session_state === 'stopped') return showOffAir();
if (session_state === 'empty_queue') return showLoading();
if (now_playing.segment_class === 'voice') return showHostCard(now_playing.host);
if (now_playing.segment_class === 'music') return showTrackCard(now_playing.title, now_playing.artist, now_playing.artwork);
```

### Lovelace custom card (sketch)

```yaml
type: custom:mammamiradio-card
endpoint: /api/integrations/v1/now-playing
poll_interval_s: 5
```

## Sample payloads

Real example responses for every segment class and degradation state live
under [`sample-payloads/`](./sample-payloads/). Each file is also a
fixture in mammamiradio's contract test suite — the docs cannot drift
from the live behavior.

## Migration from `/public-status`

If you previously polled `/public-status` for now-playing data, see
[migration-from-public-status.md](./migration-from-public-status.md) for
the field-by-field mapping. `/public-status` keeps its current shape for
the foreseeable future, so the migration is not blocking.
