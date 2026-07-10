# Fable door-unlock take

- File: `door-bentornato-fable-v2.mp3`
- Scope: local loopback station plus the mutable `homecoming` mock only. No HA Green,
  add-on, profile-routing, or deployment target was used.
- Model gate: final `banter` ledger row was `provider=anthropic`,
  `model=claude-fable-5`, `ok=true`, and `openai_fallback=false`.
- Door proof: the final ledger output contained the homecoming "bentornato" moment after
  `lock.lock_ultra_8d3c` changed from `locked` to `unlocked`.
- Capture timeline: requested banter on air at 86.4s; exact queue-ID boundary at 169.0s;
  final trim is 91.6s with an 8s music lead-in.
- Technical validation: MP3 decodes at 192 kbps; silence detection found no interval of at
  least one second. A human continuity audition was not performed by this automated run.
