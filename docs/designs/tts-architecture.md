# TTS Architecture: Voice Strategy for MammaMiRadio

Status: APPROVED (CEO review 2026-04-04)
Branch: florianhorner/feat-10x-vision

## The Core Insight

The goosebumps moment already works with flat edge-tts voices. The WTF came from
content (autoplay + song-referencing banter + timing), not voice quality. Voice is
polish, not foundation. That shapes every decision here.

## Current State

edge-tts (Microsoft Edge's free "Read Aloud" API). No API key. No account. No SLA.

**What it does well:**
- Free, zero-friction startup (Demo tier boots in 15s)
- 6+ Italian neural voices (DiegoNeural, ElsaNeural, IsabellaNeural, GianniNeural, PalmiraNeural, RinaldoNeural, FiammaNeural)
- Basic SSML prosody (rate, pitch) works for voice differentiation
- Async Python native

**What it cannot do:**
- No `mstts:express-as` emotion styles (cheerful, excited, chat). Silently ignored or bugs out.
- No voice cloning. You get what Microsoft ships.
- No Italian-accented English. Italian voices speak Italian. English voices speak American.
- No SLA. Empirically ~6% error rate at ~467 calls/day. GianniNeural fails in bursts.
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

### Rejected Alternatives

**Azure TTS free tier:** Same voices as edge-tts but with `express-as` emotion styles.
Free 500k chars/month. Rejected because: Italian express-as support is unverified
(may not exist for it-IT voices), requires Azure account (friction), and is a
dead-end dependency (still Microsoft neural voices with a ceiling).

**OpenAI TTS:** Good Italian quality, streaming API, 6 voices. $15/1M chars
(~$0.50-1/hr of radio). Rejected as default because: recurring cost for something
that should be free at scale. Could be offered as an optional premium backend later.

**ElevenLabs:** Best-in-class expressiveness and voice cloning. Free tier is
~10 minutes of audio per month (useless for continuous radio). Paid tier ($5/mo)
is tight for always-on use. Same verdict as OpenAI: optional premium, not default.

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
    |-- f5-tts  (optional, local, needs GPU + ref_audio)
    |-- openai  (optional, cloud, needs OPENAI_API_KEY)
    |-- azure   (optional, cloud, needs AZURE_SPEECH_KEY)
```

Selection logic gated on capability flags:
- No TTS config -> edge-tts
- `TTS_BACKEND=f5tts` + GPU available -> F5-TTS
- `TTS_BACKEND=openai` + key present -> OpenAI TTS
- Failure in any backend -> fallback to edge-tts -> fallback to gold clips

### Config Extension (radio.toml)

```toml
[[hosts]]
name = "Marco"
voice = "it-IT-GianniNeural"          # edge-tts fallback
ref_audio = "voices/marco_ref.wav"    # F5-TTS reference clip (optional)
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
- [ ] TTS backend selector in synthesize()
- [ ] OpenAI TTS optional backend
- [ ] Voice quality A/B comparison (edge-tts vs F5-TTS vs OpenAI)
