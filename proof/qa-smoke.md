# Runtime smoke — admin queue-from-search + Scaletta overlap

Local server (krakow-v1 code) on 127.0.0.1:8141, driven via the gstack browse binary.

## Scaletta overlap (CSS fix)
DOM extraction of `#programmeList table.a-programme tbody tr` (predicted rows):
- Each row: quando = "next"/"after that"/"later" (clean), tipo = separate badge
  ("BANTER"/"MUSIC"/"AD"/"STATION ID"), and `getComputedStyle(.ts,'::after').content === "none"`.
- Rows keep class `predicted` (dimmed). No " · planned" overflow into the type column.
- `console --errors`: (no console errors).

## Queue-from-search (background download + channel filter)
Search "nina chuba":
- BEFORE filter: 5 results, first was a channel id `UC2y0t3AAHuZxb8IgNm-A-yA` (24 chars)
  -> POST /api/playlist/add-external returned 400 invalid youtube_id format.
- AFTER filter: 4 results, all 11-char video ids; channel dropped.
- Click Queue on first result:
    POST /api/playlist/add-external -> 200 {"ok":true,"queued":"Nina Chuba – ...","status":"downloading"}
    server log: "Queueing external track (background): ... (yt:-kxTS7VGNo0)"  [instant return]
    server log ~9s later: "Queued external track: ... (yt:-kxTS7VGNo0)"      [background download pinned]
- Player page (/): renders clean, schedule overlap-free, no console errors.
