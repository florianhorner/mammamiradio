# Imaging Assets

Optional pre-recorded station imaging can be dropped here by operators.

Directory layout:

- `stingers/`: transition files named `{from}_{to}.mp3`, for example `music_banter.mp3` or `banter_music.mp3`
- `beds/`: reusable talk beds; any `.mp3` file may be sampled for banter and news underlays

If no matching asset exists, mammamiradio generates synthetic stingers and beds with FFmpeg.
Those generated fallback layers are cached under `cache_dir` as `synth_*.mp3`, keyed by
their inputs, so repeated transitions and cold-start talk beds do not rerender on every
break. Operator-provided assets still win before the cache is consulted.
