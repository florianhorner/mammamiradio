# Demo Assets

Reviewed station speech and continuity assets. Only manifest-listed, hash-matched
speech may enter the runtime; directory contents alone never authorize playback.

## Structure

- `sfx/studio/` — committed MP3 SFX used by the producer's "humanity events" (cough, paper rustle, chair creak, pen tap). These must live inside the package tree so `mammamiradio/scheduling/producer.py` and packaging find them together.
- `recovery/` — committed, package-included continuity MP3s used before any
  generated technical fallback when producer recovery or queue-drain recovery
  needs instant audio. `continuity_1.mp3` is the normal immediate clip;
  `emergency_tone.mp3` is the required 2-second cold-cache/no-clip final rung.
  Keep both under this package tree so they are available without rendering in
  standalone and Home Assistant add-on builds.
- `first_listen/` — two reviewed, content-addressed openings. Fresh unfinished
  Admin clients using `/stream?first_listen=1` hear the English
  `first_listen_admin_show.mp3`, recorded with the canonical Marco/Giulia studio
  voices. Ordinary listeners retain the 27-second Italian Edge recording
  `first_listen_show.mp3`. Each client joins the live stream afterward; these
  recordings never enter the shared rotation. Missing or rejected recordings
  fall through to live audio.
- `spoken_assets.json` — reviewed transcript, language, role, and SHA-256 for
  every MP3 inventoried under recovery/banter/first_listen/ads. Missing, changed,
  unlisted, or listener-unsafe speech fails closed. Runtime may admit approved
  recovery, mode-safe banter, and complete ads; welcome discovery stays disabled.
- `welcome/` — historical generator documentation only. The runtime no longer
  discovers welcome or unmanifested banter clips from directory contents.
- `banter/` — reviewed Normal and Super Italian recordings, with exact-predecessor
  and rare-special policies carried by their manifest entries.
- `ads/` — complete, mode-safe advertisement recordings. The release inventory
  requires seven English Normal spots and four Italian Super Italian spots.
  Each has a reviewed title, cast, transcript, SHA-256, and measured duration.
  Healthy live writing remains first choice; an approved active-mode bank can
  serve no-key and failed-generation paths without runtime LLM, TTS, or mixing.
- `music/`, `jingles/` — no bundled catalog here. The attributed starter music
  catalog lives separately under `assets/starter/`.

## Generation

`python scripts/generate-first-listen-guide.py --station-opening` renders only
that English opening (paid ElevenLabs job; approval required). It validates the
retained inventories, browser voices and station banter media before synthesis,
then validates the complete staged pack before replacing the MP3 and manifest.
Publication failures trigger rollback; if restoration also fails, backups are
retained and their directory is reported. After changing canonical voices,
regenerate the full browser pack first, then replace the Admin opening. The seven
browser guides and ordinary Italian opening are retained. Without that flag,
the generator still renders the full browser-guide pack; `--clip welcome`
replaces only its welcome. Human audition remains required before shipping.

The historical welcome-clip generator now emits neutral station-continuity
lines for local review only. Its output is not runtime-discoverable.

Author ads through the existing `synthesize_ad` pipeline, with approved paid
provider use and reviewed beds/cues. Human audition on a Mac and a small speaker
must approve the final bytes before retention; a provider fallback is not an
accepted substitute for the reviewed cast.

`scripts/validate-spoken-assets.py` gates 25–40 second ads at 48 kHz, stereo,
192 kbps MP3, -15 LUFS ±1, and at most -1 dBTP. Total silence may not exceed
25%, nor may a contiguous gap exceed 2.5 seconds. The ad bank is capped at 12 MiB; individual
files at 4 MiB, separate from the existing 40 MiB banter/opening budget.
Its package-resource checks must also run against the built
wheel/sdist or installed image, not only this source checkout. Final archive
and image proof is separate from source validation and human audition.

## Ad audition approval (2026-09-20)

Florian approved the following eleven exact masters after the final listening
review. “The Discount You Never Expected” (11) and “Small, Fierce, Parkable” (10)
are the creative headliners; playback keeps the same repetition-safe rotation.
Spot 02, “The Car That Needs Compliments,” is parked for inconsistent Italian
pronunciation and is excluded from the release inventory. Do not substitute an
older take or resume retakes without a new request. All retained voices were
rendered through ElevenLabs, with no provider fallback.

| Recording ID | Approved SHA-256 |
| --- | --- |
| 01-normal-prezzoforte-discount-complaint | `d6dad21960b455ae3a42d5f73b28d47cacea49be0dd3fd0ac24d3b0a4b7f9122` |
| 03-normal-telecuore-emotionally-unlimited | `63b0a9222c84b9188dc1502e3fc73f2c915f29a3c1236dac4d78b275f333172d` |
| 04-normal-caffe-turbino-emergency-lever | `f0a1e77e43e2800584b56f61bfb0dd7923feeef5908d7d86e9cfcd505f4233f5` |
| 05-italian-pastaforte-colosseo | `df039ca2dd207d9f9d94c5a31a9e1dcaa1a2d4a7114279f7b9fded6092dbb94e` |
| 06-italian-motoretto-parcheggiabile | `9e77e96c5a9f13701fe6bd2fe9e1b08047a0da55a7b6d5b461e902f3b6de4d36` |
| 07-italian-sacchettino-sconto | `272ff79e0788b8dc3a42350daec792e1ed5ef895ee677ece2ab58259a654e942` |
| 08-italian-prontissimo-prima-o-poi | `688b19d14c134e4c639dbbcb4cab1cb7631ebe5f72f90b154c0e85f074e1e9e8` |
| 09-normal-pastaforte-national-heritage | `21f025549fdba8f5e5a5f7da7f0ca1f7f9d586dd216bf55385a9c7e528f1fbfa` |
| 10-normal-motoretto-parkable | `b83afdec256a375e6758304770b4af7d90a73599ab78482b154df6f8319fc0f5` |
| 11-normal-sacchettino-unexpected-discount | `bedf7622be55e68c12d7e9225f145df4c0223aa443d626e656d656abec20a06d` |
| 12-normal-prontissimo-eventually | `d836deb3ec64ce0cb89a9bb70164658570d6d58c37670b5a8d25f8ebf872fb95` |

## Licensing

Any future music tracks must be CC-licensed or original compositions. Speech
transcripts are original station copy; do not infer a recording's provider or
voice provenance from its category or filename.
