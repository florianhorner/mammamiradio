# Changelog (test fixture — must pass lint clean)

## [1.2.3] - 2026-05-03

### Added

- Italian-trending music as the default Jamendo source. New `jamendo_country` and `jamendo_order` fields in `[playlist]` (config.yaml). Backwards compatible — existing configs without these fields keep their prior behavior.
- New `/og-card.png` route returns a 1200x630 social preview rendered from the active brand theme. Falls back to the logo SVG on render failure.

### Fixed

- Listener mobile header overflowed phone viewports. Three layered fixes restored vertical scroll and disabled iOS Safari address-bar collapse cutting the bottom of the page.
- Producer wakes immediately on session resume. Replaced 1-second poll with `asyncio.wait_for(resume_event.wait(), timeout=1.0)`. Resume lag drops from worst-case 1s to milliseconds.

### Security

- Stored XSS in admin panel via Home Assistant entity state values. Five HA-sourced fields are now wrapped with `esc()` before assignment.
- Content-Security-Policy on `/admin` now uses `script-src 'self' 'unsafe-inline'`, blocking external script injection.

### Changed

- Subpackage restructure of the python module tree. Flat files moved into `core/`, `audio/`, `playlist/`, `hosts/`, `home/`, `scheduling/`, and `web/`. Public addon entrypoint `mammamiradio.main:app` is unchanged.

**Contributors:** [@ashika-rai-n](https://github.com/ashika-rai-n)
