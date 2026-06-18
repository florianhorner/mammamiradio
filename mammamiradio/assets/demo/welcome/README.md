# Welcome Clips

Short greetings where the DJ "interrupts" the broadcast to welcome a listener.
They are one of the station's instant-audio fallbacks: the playback loop reaches
for them via `_pick_canned_clip("welcome")` to keep sound flowing during queue
recovery, so a listener hears a warm greeting instead of a gap.

## Generating clips

Run from the project root with the venv active:

```bash
python scripts/generate_welcome_clips.py            # write missing clips
python scripts/generate_welcome_clips.py --dry-run  # list, write nothing
python scripts/generate_welcome_clips.py --overwrite # rebuild all clips
```

The clip contract (filenames, voices, lines) lives in `scripts/generate_welcome_clips.py`.
It defaults to the free Edge engine, so no API key is required. Listen to the output, then
commit the MP3s if they sound right — the runtime reaches for them via `_pick_canned_clip("welcome")`
as an instant-audio fallback.

These clips are Italian-only by design (matches station identity).
