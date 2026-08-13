# TTS Architecture: Voice Strategy for Mamma Mi Radio

Status: APPROVED (CEO review 2026-04-04)
Branch: florianhorner/feat-10x-vision

## The Core Insight

The goosebumps moment already works with flat edge-tts voices. The WTF came from
content (autoplay + song-referencing banter + timing), not voice quality. Voice is
polish, not foundation. That shapes every decision here.

## Current State

The station supports a tiered voice stack: Edge Read Aloud for the no-key path,
OpenAI for promptable voices, Azure Speech for official Italian voices, and
ElevenLabs for operator-provided character voices.

edge-tts remains the free fallback (Microsoft Edge's free "Read Aloud" API). No
API key. No account. No SLA.

**What it does well:**
- Free, zero-friction startup (Demo tier boots in 15s)
- Four Italian neural voices in the installed package today: DiegoNeural, ElsaNeural, GiuseppeMultilingualNeural, IsabellaNeural
- Basic SSML prosody (rate, pitch) works for voice differentiation
- Async Python native

**What it cannot do:**
- No `mstts:express-as` emotion styles (cheerful, excited, chat). Silently ignored or bugs out.
- No voice cloning. You get what Microsoft ships.
- No Italian-accented English. Italian voices speak Italian. English voices speak American.
- No SLA. Empirically throttles/fails under sustained use; official Azure Speech is the production-grade Microsoft path.
- Requires internet. Not local.

**Impact on product:** The "sounds like an actual radio station" moment at the Full AI
Radio tier is limited by flat, neutral delivery. Listeners notice the robotic affect
within 10 seconds. The content compensates, but the voice ceiling is real.

## Decision: Tiered TTS Strategy

### On Air tier (no keys)

**Engine: edge-tts + pre-generated gold clips**

15 pre-bundled Italian banter clips in `demo_assets/banter/`. These are the shareware
trial: high-quality personality showcase that degrades to TTS fallback after exhaustion.
Edge-tts handles all live synthesis. No API keys, no GPU, no cost.

The gold clips bank is also the reliability safety net. If edge-tts goes down,
`_pick_canned_clip()` rotates through cached audio. The station never goes silent.

### Your Station tier (Spotify connected)

**Engine: edge-tts (same)**

The upgrade here is music, not voice. Edge-tts handles the welcome banter
(generated in parallel with autoplay capture). The personalized content
("They just played Lazza... Cenere...") carries the moment, not the voice quality.

### Live Broadcast tier (Anthropic key)

**Engine: edge-tts now, F5-TTS when validated**

Users who supply an Anthropic key have opted into the premium experience. They
deserve expressive voices. But the current priority is shipping the 10x vision,
not swapping TTS backends. Edge-tts works. It's flat, but it works.

**Future upgrade: F5-TTS (self-hosted voice cloning)**

F5-TTS is a flow-matching model that clones any voice from a 5-15 second reference
clip. The emotion is baked into the reference audio, not controlled via tags.
This maps perfectly to the host architecture:
- Record Marco speaking enthusiastically for 10 seconds -> F5-TTS clones that energy
- Record Giulia deadpanning for 10 seconds -> F5-TTS clones that dryness
- The `style` descriptions in radio.toml become casting notes, not prompt engineering

Requirements for validation:
- GPU or Apple Silicon MPS
- Italian language quality verification at production scale
- Inference latency testing (target: <3s with queue lookahead)
- Reference audio sourcing (record or find CC-licensed Italian radio samples)

### Cloud Backends

**Azure Speech:** Official Microsoft Speech API used for production-grade
Italian voices beyond the small `edge-tts` Read Aloud catalog. Requires
`AZURE_SPEECH_KEY` and `AZURE_SPEECH_REGION`; every configured Azure voice must
also carry an Edge fallback.

**OpenAI TTS:** Promptable voice path selected by `[tts.openai]` in the canonical
`model_registry.toml`. The voice catalog includes `marin` and `cedar` in addition
to the earlier built-ins. Good for host and ad character direction; falls back to
Edge when `OPENAI_API_KEY` is absent or the request fails.

**ElevenLabs:** Optional custom character-voice backend for ads, guest bits, and
station imaging. Requires `ELEVENLABS_API_KEY` and operator-provided voice IDs;
the default station config does not ship fake ElevenLabs IDs.

**Bark (Suno):** Expressive (laughter, music in text markup) but 5-15s per clip.
Too slow for live radio. Abandoned (no updates since 2023).

**Piper TTS:** Blazing fast (~50ms CPU), tiny footprint, offline. But monotone and
robotic. Worse than edge-tts for personality. Good as emergency offline fallback only.

## Architecture

`synthesize()` in `tts.py` is the single entry point. Adding a backend selector
is a clean S-sized change:

```
synthesize(text, voice, output_path, *, rate=None, pitch=None)
    |
    |-- edge-tts (default, free, always available)
    |-- openai  (optional, cloud, needs OPENAI_API_KEY)
    |-- azure   (optional, cloud, needs AZURE_SPEECH_KEY + AZURE_SPEECH_REGION)
    |-- elevenlabs (optional, cloud, needs ELEVENLABS_API_KEY)
```

Selection logic gated on capability flags:
- No TTS config -> edge-tts
- `engine="openai"` + key present -> OpenAI TTS
- `engine="azure"` + key/region present -> Azure Speech TTS
- `engine="elevenlabs"` + key present -> ElevenLabs TTS
- Failure in any cloud backend -> configured Edge fallback -> silence/clip recovery

### Config Extension (radio.toml)

```toml
[[hosts]]
name = "Marco"
voice = "cedar"
engine = "openai"
edge_fallback_voice = "it-IT-GiuseppeMultilingualNeural"
```

### Reliability Chain

```
Claude generates banter text
    |
    v
TTS backend synthesizes audio
    |-- success -> queue segment
    |-- failure -> retry once
         |-- success -> queue segment
         |-- failure -> pick canned clip from demo_assets/banter/
              |-- success -> queue segment
              |-- empty -> TTS fallback one-liner ("E torniamo alla musica!")
                   |-- success -> queue segment
                   |-- failure -> skip banter, play next music
```

The station never goes silent. Four fallback layers.

## Edge-TTS Operational Notes

- GianniNeural fails in bursts. DiegoNeural is more reliable as fallback.
- Rate limiting appears around ~467 calls/day (~6% error rate).
- The library is a reverse-engineered browser endpoint. Microsoft can change it anytime.
- No SSML emotion tags. Only `rate` and `pitch` prosody work.
- Output is MP3 24kHz mono. Normalized to station bitrate via FFmpeg.
- `_prosody_for_host()` in tts.py derives rate/pitch from personality axes.

## Implementation Status

- [x] edge-tts integration with prosody (rate/pitch per host)
- [x] 15 pre-generated gold banter clips (demo_assets/banter/)
- [x] Canned clip rotation with anti-repeat (_pick_canned_clip)
- [x] SFX allowlist validation (LLM path traversal fix)
- [x] Banter TTS failure graceful skip
- [ ] F5-TTS prototype branch
- [ ] Reference audio recording/sourcing for Marco + Giulia
- [x] Provider-routed TTS backend selector in synthesize()
- [x] OpenAI TTS optional backend
- [x] Azure Speech optional backend
- [x] ElevenLabs optional backend
- [x] Voice quality A/B comparison via `scripts/audition_tts_voices.py` (edge-tts vs OpenAI vs Azure vs ElevenLabs)
