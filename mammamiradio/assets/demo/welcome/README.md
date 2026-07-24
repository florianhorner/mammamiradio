# Historical Welcome-Clip Generator

This directory is not a runtime audio source. Connection-edge greetings cannot
truthfully establish that a person arrived or returned, so queue recovery uses
only reviewed, hash-bound speech declared in `../spoken_assets.json`.

## Generating clips

Run from the project root with the venv active:

```bash
python scripts/generate_welcome_clips.py            # write missing clips
python scripts/generate_welcome_clips.py --dry-run  # list, write nothing
python scripts/generate_welcome_clips.py --overwrite # rebuild all clips
```

The retained generator emits neutral station-continuity lines for local audio
review. Generated files do not air merely because they exist here.
