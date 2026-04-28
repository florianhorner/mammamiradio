# Codex review findings (post-diff)

codex exec, model_reasoning_effort=high, sandbox=read-only

## Round 1: 2 P1 findings

P1-1: Track.cache_key shape change broke chart/youtube tracks without youtube_id (legacy fallback only triggered when youtube_id was set).
P1-2: Legacy fallback for non-jamendo non-youtube sources could replay old chart cache for local/demo tracks (cross-source contamination).

Both fixed: legacy fallback now applies ONLY when track.source == "youtube", with or without youtube_id. Local and demo tracks are served via local_path / demo_assets directly.

## Round 2: 0 P0, 0 P1

P2 nit only (corrupt legacy cache wouldn't self-heal — accepted: legacy path is one-time post-deploy migration window).
