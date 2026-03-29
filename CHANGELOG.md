# Changelog

This file summarizes what shipped in this repo so a human can scan it without reading 20 commits in `git log`.

## 0.1.1 - 2026-03-29

### Added

- Home Assistant context injection for banter and ads, so hosts can reference live ambient home state when configured.
- Dedicated architecture, operations, contributing, and troubleshooting docs.
- In-code documentation across the Python app modules, state models, and runtime entry points.

### Changed

- README and agent docs now match the actual runtime behavior, auth model, and fallback paths.

## 0.1.0

Initial usable release of `fakeitaliradio`.

### Core station

- FastAPI app with a continuous MP3 stream and shared live playback timeline.
- Segment scheduler for music, banter, and ads.
- Producer/consumer queue model with recovery via silence segments on transient failures.
- Playlist support from Spotify playlists or liked songs, with demo fallback.

### Streaming and UI

- Control-plane dashboard at `/`.
- Public listener page at `/listen`.
- Public and admin JSON status surfaces.
- Stream pacing throttled to bitrate so the dashboard matches what listeners hear.

### Spotify integration

- go-librespot capture via FIFO.
- Persistent FIFO drain to avoid macOS `ENXIO` playback skips.
- Auto-transfer support to the `fakeitaliradio` Spotify device.
- `start.sh` workflow so go-librespot survives hot reload.

### Audio generation

- Claude-written banter between configurable hosts.
- Fake ad generation with recurring brands, voice actors, bumper jingles, SFX, and music beds.
- Edge TTS synthesis for hosts and ads.
- FFmpeg normalization and assembly pipeline.

### Controls and debugging

- Queue controls: shuffle, skip, purge, remove, move, play-next.
- Debug panel for go-librespot logs and producer errors.
- Improved structured debug log readability.

### Stability

- Config validation with fast failure on invalid setups.
- Recovery paths for producer exceptions and deprecated event loop usage.
- Additional test coverage for config, models, scheduler, playlist, ads, and preview behavior.
