# Migrating from `/public-status` to `/api/integrations/v1/now-playing`

If you've been polling `/public-status` to render mammamiradio in a third-
party UI (Music Assistant provider, custom HA card, etc.), the v1
contract gives you a stable shape that's allowed to evolve without
breaking listener-facing UI.

`/public-status` keeps its current shape — migration is not time-critical.
But every new third-party integration should target the v1 contract from
day one.

## Field-by-field mapping

| `/public-status` field | v1 contract field | Notes |
| --- | --- | --- |
| `station` (string) | `station.name` (string) | v1 nests under a `station` block with frequency, theme, hosts. |
| `brand.frequency` | `station.frequency` | Lifted to top of the station block. |
| `brand.hosts` | `station.hosts` | Same item shape (engine_host, display_name, description). |
| `now_streaming.type` | `now_playing.segment_type` | Carried through unchanged for diagnostics. **Do not branch on this for core UX** — branch on `segment_class` instead. |
| (none) | `now_playing.segment_class` | **New.** Stable display bucket: `music`/`voice`/`interstitial`/`unavailable`. |
| `now_streaming.label` | `now_playing.title` | Trimmed; `title_only` preferred when present. |
| `now_streaming.started` | `now_playing.started_at` | Same epoch float, renamed. |
| `now_streaming.duration_sec` | `now_playing.duration_estimate_sec` | Renamed; emits `null` when unknown instead of `0`. |
| `now_streaming.metadata.artist` | `now_playing.artist` | Lifted; only populated for `segment_class: "music"`. |
| `now_streaming.metadata.album_art` | `now_playing.artwork` | Renamed; only populated for music. |
| `now_streaming.metadata.album` | `now_playing.album` | Lifted; music-only. |
| `now_streaming.metadata.spotify_id` | `now_playing.external_ids.spotify` | Moved into a provider→id map. The `_id` suffix is gone. |
| `now_streaming.metadata.youtube_id` | `now_playing.external_ids.youtube` | Same — provider keyed, no `_id` suffix. |
| `now_streaming.metadata.host` | `now_playing.host` | Lifted; only populated for `segment_class: "voice"`. |
| `now_streaming.metadata.*` (other internal fields) | (filtered) | **Internal fields no longer leak.** Allowlist locks the set of forwarded keys. |
| `upcoming[]` | `up_next[]` | Renamed; shape simplified. |
| `upcoming[].type` | `up_next[].segment_type` | Same rename. |
| `upcoming[].label` | `up_next[].title` | Same rename. |
| `upcoming[].source` (`"rendered_queue"`/`"predicted_from_playlist"`) | `up_next[].predicted` (bool) | Reshaped to a single bool. `false` = queued for real, `true` = scheduler's best guess. |
| `session_stopped` (bool) | `session_state` (`"live"`/`"stopped"`/`"empty_queue"`) | Lifted to an enum that distinguishes "stopped" from "still booting / empty queue". |
| `stream.audio_format` | `stream.audio_format` | Same shape. |
| (none) | `stream.relative_url` | **New.** Canonical path component; resolve against your mammamiradio host. |
| (none) | `stream.absolute_url` | **New, optional.** Present only outside HA Supervisor ingress. |
| (none) | `schema_version` | **New.** Always `"1"` while the v1 contract is active. |
| (none) | `changed_at` | **New.** Use with `If-None-Match` for cheap polling. |

## Header behavior changes

| Header | Before | After |
| --- | --- | --- |
| `ETag` | Not set | Weak ETag set on every response. |
| `Cache-Control` | Not set | `public, max-age=2`. |
| `If-None-Match` (request) | Ignored | Honored — returns `304 Not Modified` when state hasn't moved. |

## Worked example

Old code:

```python
import requests
r = requests.get("http://host:8000/public-status")
data = r.json()
now = data["now_streaming"]
if now and now.get("type") == "music":
    title = now["metadata"].get("title_only") or now["metadata"].get("title")
    artist = now["metadata"].get("artist")
    art = now["metadata"].get("album_art")
    show_track(title, artist, art)
```

New code:

```python
import requests
r = requests.get("http://host:8000/api/integrations/v1/now-playing")
data = r.json()
np = data["now_playing"]
if np and np["segment_class"] == "music":
    show_track(np["title"], np["artist"], np["artwork"])
elif np and np["segment_class"] == "voice":
    show_host_card(np["host"])
elif data["session_state"] == "stopped":
    show_off_air()
```

## Drift detection

mammamiradio's CI runs the existing MA drift guard against `/public-status`
(see `tests/web/test_public_status_contract.py`) so the legacy consumer
contract stays stable while you migrate. The v1 contract has its own
drift guard at `tests/integrations/test_now_playing_contract.py`.
