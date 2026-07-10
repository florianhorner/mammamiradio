# Recorded Night Drive imaging pack

This is Mamma Mi Radio's bundled, public radio-imaging library: the material
for station IDs, sweepers, time checks, handoffs, ad breaks, spoken beds, and
fictional-ad scenes. It is intentionally made from real recorded details —
crowd laughter and applause, trumpet, mandolin, café and espresso work, cash
register, telephone, cassette, cocktail ice, and a passing car — rather than
an interchangeable pack of synthesized sweeps and drones.

Every source recording in the public package is **CC0 1.0**. The committed
`manifest.json` and generated `ATTRIBUTION.md` name the creator and source page,
record the exact source SHA-256 and every delivered MP3 checksum, and describe
the edit made for the station. The source masters are deliberately not checked
in: they are larger curator inputs, while the rendered clips and their audit
ledger are the shipped public work.

## What ships

| Surface | Material |
| --- | --- |
| Station identity | `station_id.mp3`, `sweeper.mp3`, `time_check.mp3` — real brass, mandolin, glass and cassette texture |
| Handoffs | `stingers/` and `bumpers/` — recorded transitions, not synthesized whooshes |
| Ordinary spoken bed | `beds/casa_notte.mp3` — low café room tone |
| Legacy SFX names | `sfx/` retains existing operator-facing names, but each file is now a recorded compatibility cue |
| Ad scene library | `ads/beds/` and `ads/cues/` — 60 total delivered clips, including distinct applause, laughter, trumpet, till, phone, tape, espresso, ice, and road variants |

The nine named ad scenes live in `manifest.json`: `cafe_testimonial`,
`stadium_win`, `showroom_reveal`, `bureaucracy_stamp`, `motorway_pass`,
`late_night_hotline`, `supermarket_dash`, `pharmacy_whisper`, and
`home_reveal`.

Each scene has a strict production cap: **one quiet bed plus at most two dry
foreground cues**. The renderer maps `intro`, `after_first_voice`, `mid`, and
`outro` to the rendered dialogue, then makes one bounded cue mix before the
normal broadcast master. `after_first_voice` includes any leading pause and the
short joins inserted between rendered ad parts. The runtime and offline
validator share this canonical schema: every recipe uses explicit `asset_id`
and `gain_db` fields; aliases such as `oneshots` and `bed_candidates` are not
accepted.

It never downloads, synthesizes, or renders source audio on the live station
path. A configured recipe suppresses legacy accents only after it resolves. If
it cannot resolve, the script is written through the complete legacy sonic
path instead. A missing safe file disables only the recipe that needs it; the
offline public-pack validator still rejects any missing declared delivery asset
before release.

## Runtime selection

With the standard configuration (`[imaging].assets_dir = ""`),
`ImagingLibrary` selects this packaged directory. Root `radio.toml` and the
Home Assistant add-on's `radio.toml` map every shipped fictional brand to one
of the reviewed scenes. Recipe-driven spots suppress legacy generated
brand-motifs and LLM-requested generic SFX, so the scene remains authored and
bounded. When `use_music_queue_for_beds` is enabled, an eligible adjacent song
supplies the spoken bed; Casa Notte is the recorded fallback when no adjacent
song is safe to reuse.

An operator-provided `assets_dir` still replaces the whole pack. Custom legacy
campaigns with no `sonic_recipe` keep their existing fallback behaviour; an
incomplete custom pack cannot block a spot from airing.

## Validate, rebuild, and audition

Normal CI and review validate the public ledger offline:

```bash
.venv/bin/python scripts/validate_audio_asset_pack.py
```

To rebuild from the separately archived reviewed masters, first verify their
checksums and then render the pack. The builder never fetches a file:

```bash
.venv/bin/python scripts/build_public_imaging_pack.py \
  --source-dir /path/to/reviewed-cc0-masters
.venv/bin/python scripts/validate_audio_asset_pack.py --write-attribution
```

The retired `scripts/generate_sonic_brand_assets.py` command is now a safe
compatibility wrapper: `--validate-only` checks the recorded pack; a rebuild
requires `--source-dir` and cannot silently recreate procedural audio.

For a local listening review, no TTS provider or live station is needed:

```bash
.venv/bin/python scripts/audition_sonic_brand.py
```

It writes a timestamped `index.html` under `tmp/sonic-brand-auditions/`. The
top half compares each legacy procedural identity surface to the recorded
replacement. The lower **Ad scene recipes** board plays all nine real-bed plus
real-cue timelines, with each cue's editorial anchor shown next to the player.
Use `--no-recipe-previews` only for a quick identity A/B.

## Deliberate boundaries

This pack is normal-programme material. It does not replace the packaged
recovery assets under `mammamiradio/assets/demo/`, and it does not change the
independent `[audio].broadcast_chain` setting. Those continuity and transmitter
choices stay separate from this sound-library refresh.
