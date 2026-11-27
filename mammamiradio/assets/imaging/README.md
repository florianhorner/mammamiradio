# Modern Night Drive imaging pack

This is Mamma Mi Radio's bundled radio-imaging library. Its Neon Relay station
signature carries the atmospheric character of Velvet Horizon; the signature
appears only in the station ID and sweeper. Music-to-speech and
speech-to-music handoffs are separate, and advertising uses distinct `in`,
`mid`, and `out` bumpers.

The pack contains 47 delivery assets and nine semantic advertising recipes.
All material is project-authored deterministic audio: no trumpet or novelty
mandolin recording is part of the delivered sound. Compatibility filenames
remain stable for operators, while sounds such as `startup_synth` are genuinely
electronic.

## Provenance and approval

`manifest.json` is the self-contained runtime contract. It records every
asset, retained source master, exact layer timing/gain/DSP/license fact, and an
exact file inventory. `ATTRIBUTION.md` is generated from that manifest. The
separate candidate board and its digest-bound listening receipt authorize this
exact immutable runtime projection before promotion.

`provenance/generator-src/` keeps a byte-exact copy of every generator input
the manifest pins, stored at its repository path. Validation hashes those
copies, so the pack proves which bytes generated it in any checkout, with no
Git history or tags. The Git revision recorded in the manifest is a reference
only. The directory is provenance evidence, not runtime payload: it sits
outside the runtime file inventory and is left out of the Docker and add-on
images.

Validate the installed pack offline with:

```bash
.venv/bin/python scripts/validate_audio_asset_pack.py
```

The recovery and First Listen audio under `mammamiradio/assets/demo/` and the
web static tree are separate and are not replaced by this pack.
