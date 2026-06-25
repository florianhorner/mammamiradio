# Home Assistant Privacy And Upstream Proposals

This document records shipped mammamiradio behavior and two upstream-facing
proposal sketches. Proposal sections are not shipped Home Assistant behavior.

## Local Privacy Model

When Home Assistant context is enabled, mammamiradio reads a filtered HA state
snapshot before banter, ad, and news-flash generation. It drops telemetry/config
entities, unavailable states, free-text helpers, trackers, cameras, alarms, and
precise person location attributes before prompt assembly. Resident presence is
kept as home/away only.

If label generation is active, entity names and room assignments for
non-sensitive entities may be sent to Anthropic once to produce radio-friendly
labels. Sensor values, presence, and location are not included in that label
request. Results are cached locally in `cache/ha_label_catalog.json`.

The provenance ledger is local and optional outside the add-on. In the HA add-on
it writes best-effort JSONL rows under `/data/cache/ledger/`; standalone uses the
configured `MAMMAMIRADIO_CACHE_DIR`. Rows can include prompt inputs, generated
scripts, final aired outcomes, and operator toggles. Disable it with
`MAMMAMIRADIO_LEDGER_ENABLED=false` outside the add-on; inspect it by reading the
ledger files directly on the host that runs the station.

## Proposal: Add-on And Integration Entity Ownership

Problem: an add-on can push an entity over the REST states API while a HACS or
core integration also registers the same entity id. HA state is last-writer-wins,
so the dashboard can flap between a ghost state and a registered entity.

Proposal: Home Assistant should detect when a REST-pushed state collides with a
registered entity from an integration and surface a Repair that names both
owners. For add-ons, Supervisor could optionally expose a standard capability
flag that tells the add-on to stop pushing compatibility ghosts once the
registered integration is installed.

Mammamiradio local behavior: new add-on installs default the ghost media-player
push off; legacy installs preserve the old default until the operator changes
the option. The HACS integration raises a Repair when it sees a legacy
`media_player.mammamiradio` conflict.

## Proposal: Assist And AI Context Privacy

Problem: AI speech features can use HA context to feel natural, but users need
clear boundaries for consent, redaction, provenance, and local storage.

Proposal: HA should standardize an AI-context contract that lets integrations
declare which entity classes are eligible, which attributes are redacted, where
derived context is stored, and how generated speech can cite or expose its
source. Assist surfaces should show whether a response used live home context,
cached labels, or no HA context.

Mammamiradio local behavior: sensitive domains and attributes are filtered
before prompts, label generation has a narrower payload than script generation,
and the ledger records provenance locally so the operator can inspect how an
aired moment was made.
