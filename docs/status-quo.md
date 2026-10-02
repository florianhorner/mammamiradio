# Mamma Mi Radio: installable release

Snapshot: **1 October 2026**. This page describes published **3.0.0**.

## Published today

- [GitHub release v3.0.0](https://github.com/florianhorner/mammamiradio/releases/tag/v3.0.0)
  was published on 1 October 2026 as a stable release.
- At this snapshot, `main` and tag `v3.0.0` point to `bde86cee`.
- Package and stable add-on versions are `3.0.0`. The stable `3.0.0` images
  exist for both `amd64` and `aarch64`.
- First Listen plays before any writing key or Home choice. The offline starter
  rotation has twelve credited tracks, with recorded host breaks and finished
  fictional ads. Missing writing keys do not deliberately make the station silent.
- Jamendo stays off until enabled. Fresh installations keep Home private;
  explicit sharing begins with daylight and one weather source. Laundry and
  arrival sharing need later Home Profile support.
- Music Assistant stable 2.10 includes the provider, still marked **alpha**.
  See the [integration guide](integrations/ha-integration.md#play-it-through-music-assistant).
- Edge `5177195` predates this release. Use stable for daily listening.

First Listen, the starter collection, host-only now-playing, ad receipts, and
the empty-crate fix have shipped. Both changelogs have empty Unreleased sections.

## Next update

The private-profile compatibility check and permanent narrow-consent record are
unreleased work. They are not features of published 3.0.0. Read the
[upgrade note](../ha-addon/mammamiradio/DOCS.md#upgrade-note-next-update-not-part-of-300)
for eligibility, failure behavior, and the explicit Home choice.

## Install and listen

Use the [README](../README.md#first-listen) or the
[add-on instructions](../ha-addon/mammamiradio/DOCS.md). HACS installation uses a
custom repository. The Studio B films remain attached to the
[v2.18.0 release](https://github.com/florianhorner/mammamiradio/releases/tag/v2.18.0);
v3.0.0 has no release assets.
