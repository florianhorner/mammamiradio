# Mamma Mi Radio — "the coffee knew" (showreel sample)

**The file:** `ma-pr-3836.mp3` — ~1:45, captured as **one continuous slice of the live
stream** (no editing, no stitching). It moves through a song → a host break → an ad break.
It exists to make audible the one thing a *Music Assistant provider* gives you that a
pasted stream URL can't.

---

## The 90-second read — timestamps → what the provider surfaces

| time | you hear | the provider emits to Music Assistant |
|------|----------|----------------------------------------|
| `0:00` | a song fading out | `segment_class: music` · *Night in Venice* (CC0 bed) |
| `0:08` | Marco & Giulia talking | `segment_class: voice` · *Marco & Giulia* / Marco del bar, Nonna Giulia |
| `0:51` | an ad break | `segment_class: interstitial` · *Ad: Caffè Turbino* |

Those are **typed segments**. The provider tells Music Assistant whether the current
moment is music, a human voice break, or an interstitial — so the now-playing card shows
the right thing as the station moves from a song to banter to an ad. A manual stream URL
shows one frozen label for all of it. *That* is why this is a provider, not a bookmark.

---

## What came together here (three layers, bottom to top)

**1. Segment-aware metadata — the engineering reason.**
The clip crosses all three classes the provider distinguishes (music / voice / interstitial)
in 105 seconds. This is the same contract the PR's live `/api/integrations/v1/now-playing`
capture documents, made hearable.

**2. A home-aware "impossible moment" — the product reason.**
The hosts notice the coffee machine just switched on, the kitchen window is open, and rain
is on the way — and fold it into the chatter. On a home that's actually connected, that's
your **real** Home Assistant state (strictly read-only: the station *notices*, it never
*acts*). For this sample the home is staged — a mock HA feeding a "coffee brewing" scene —
and the household details are fictional, but the awareness itself is the real engine
deriving a mood from home state and handing it to the writer. A stream URL can't know your
kitchen.

**3. Emergent character — the part nobody wrote.**
This is the good one. The writer (Claude Opus 4.8) took Giulia's deadpan correction streak
— *"Nona settimana. Non. È. Di. Venezia."* ("ninth week — it is NOT from Venice") — and,
unprompted, **scored it like a sport**: the coffee machine firing becomes *"il campanello
della vittoria"* (the victory bell), Marco *"ha appena perso Venezia"* (just lost Venice
again), and Giulia signs off *"lui perde, voi vincete il caffè"* (he loses, you win the
coffee).

Nobody templated that. The provenance ledger confirms it: the Callback Director (the engine
that deliberately plants running gags) logged `gag_offered: false` for this segment. The
sporting frame is the model riffing off two personas in a room — which is exactly the
texture that makes listeners stop asking whether it's real radio.

---

## How it was made (staged, but real)

Not a mock-up. Every second is the **actual production audio pipeline** — the real host
voices, the real loudness/processing, the producer's own song→talk transition and ad-break
bumpers. What's staged is the *inputs*: the song was chosen, the segment order was forced,
and the home scene was set. Then it was recorded as **one continuous take** — so the seam
at ~0:51 (Marco handing into the ad) is the station's own transition, not a cut.

The capture tool is reusable: a small harness drives a local instance, stages a Home
Assistant scene, forces the segment order, and records the live stream. New host, new
section, new mode → new scene file → new snippet. (See `scripts/showreel/README.md`.)

---

## Lift-and-use blurbs

**For the Music Assistant PR / forum (maintainer-facing):**
> Sample audio (one continuous capture, no editing): a song → host banter → an ad break,
> ~1:45. It makes the provider's typed metadata audible — `music` at 0:00, `voice` at 0:08,
> `interstitial` at 0:51 — the same `segment_class` contract documented in the live capture
> above. The banter also folds in live home context (the listener's coffee machine, an open
> window, incoming rain), read-only, which is the content reason a provider beats a manual
> stream URL.

**For the blog / GitHub forum (story-facing):**
> We staged a 90-second sample of the station and the writer did something we didn't ask
> for. Two AI hosts were arguing about whether a song was "from Venice" (ninth week running),
> and when the listener's coffee machine switched on, the model spontaneously turned the
> whole thing into a sport — *"the victory bell rang, you lost Venice again, he loses, you
> win the coffee."* The provenance ledger proves the running-gag engine was switched off:
> it just… invented that. This is the bet behind Mamma Mi Radio — a station that knows a
> little about your home and riffs like a real one. Here's the clip, and here's exactly how
> it came together.

---

*Household details in the home scene are fictional. Music bed is CC0 (FreePD, public
domain). Ad brand "Caffè Turbino" is a fictional brand from the station's config.*
