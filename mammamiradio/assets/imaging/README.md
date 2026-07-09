# Italian Night Drive imaging pack

This is Mamma Mi Radio's bundled default imaging pack: the sonic material for
ordinary station IDs, sweepers, time checks, transitions, ad breaks, spoken
beds, and ad SFX. With the standard configuration (`[imaging].assets_dir = ""`),
`ImagingLibrary` selects this directory at runtime.

Both the root `radio.toml` and the Home Assistant add-on's `radio.toml` leave
`assets_dir` empty, and both package this Python tree. They therefore use the
same Night Drive pack without an add-on-specific copy or setting.

## Pack contents

| Surface | Assets |
| --- | --- |
| Station identity | `station_id.mp3`, `sweeper.mp3`, `time_check.mp3` |
| Music/speech transitions | `stingers/music_to_speech.mp3`, `stingers/speech_to_music.mp3` |
| Ad handoff | `bumpers/ad_break.mp3` |
| Spoken and ad bed | `beds/casa_notte.mp3` |
| Ad SFX bank | `sfx/cash_register.mp3`, `chime.mp3`, `ding.mp3`, `hotline_beep.mp3`, `ice_clink.mp3`, `mandolin_sting.mp3`, `register_hit.mp3`, `startup_synth.mp3`, `sweep.mp3`, `tape_stop.mp3`, `whoosh.mp3` |

`manifest.json` is the complete inventory and provenance record. It declares
the package format (48 kHz stereo MP3 at 192 kbps), the intended duration and
purpose of every asset, and the license/provenance for each file. It is an audit
manifest; runtime asset lookup uses the filenames above directly.

Every asset is Apache-2.0 and was created by deterministic FFmpeg procedural
synthesis in `scripts/generate_sonic_brand_assets.py`; the pack contains no
downloaded or external samples.

## Runtime selection and fallback

`[imaging].assets_dir` chooses one imaging root:

- Empty (the default) selects this packaged directory.
- A non-empty custom path replaces the packaged root; it is not an overlay. If
  an operator supplies an incomplete custom pack, a missing asset falls back to
  the existing procedural renderer rather than silently taking the packaged
  counterpart.

Within the selected root, station-ID, sweeper, time-check, and ad-bumper calls
copy their matching file first, then use their existing procedural generator.
Transition stingers first try an exact
`stingers/{from}_{to}.mp3`, then the direction-level `music_to_speech.mp3` or
`speech_to_music.mp3`, then a cached synthetic stinger. Talk beds prefer any
`beds/*.mp3` in the selected root, then an adjacent music source when
`use_music_queue_for_beds` allows one, then the cached synthetic drone.

Ad selection has two intentional special cases:

- A configured `[ads].sfx_dir` wins only when it is a real directory; otherwise
  the selected imaging root's `sfx/` bank is used before procedural SFX.
- Ad music beds use a mood-matching filename when present, then
  `beds/casa_notte.mp3`, then another bundled bed. With no usable selected-root
  bed, the existing synthetic ad-bed path remains in service.

## Local A/B audition

From the repository root, create a local listening comparison with:

```bash
.venv/bin/python scripts/audition_sonic_brand.py
```

The command writes a timestamped directory under
`tmp/sonic-brand-auditions/`, including an `index.html` listening page,
`manifest.json`, and procedural-baseline versus Night Drive files for the
station ID, sweeper, time check, both transition directions, ad bumper, and
Casa Notte talk bed. Open the generated `index.html` in a browser to review it.

Use `--output-dir` to choose a different parent directory and `--timestamp
YYYYMMDDTHHMMSSZ` for a reproducible review path. The command validates the full
pack before writing a run and never calls a TTS provider, starts the station, or
touches its playback queue.

## Deliberate boundaries

This pack is normal-programme source material. It does not replace the packaged
recovery assets in `mammamiradio/assets/demo/`: bridge and rescue segments keep
their instant-audio behavior and explicitly skip the egress FX pipeline.

The pack also does not enable or configure the FM broadcast chain. That remains
the independent `[audio].broadcast_chain` choice (off by default); when enabled,
the chain colours an already-produced normal segment after its Night Drive
material has been mixed in.
