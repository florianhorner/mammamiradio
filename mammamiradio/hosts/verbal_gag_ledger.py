"""Verbal running-gag ledger — rare cross-domain callbacks for banter gags.

The companion to ``EveningLedger`` (home/evening_memory.py). EveningLedger
surfaces HA-event running gags into banter; this ledger lets a VERBAL gag planted
in DJ banter resurface ONCE inside an unrelated news flash or ad — the
cross-domain "callback" that makes a listener sit on the edge of "eeerm... what
was that". It reuses EveningLedger's weighted-pick-with-silence primitive
(``gag_select.weighted_offer``) so rarity behaves identically.

Rarity is structural, not model whim:

    seed (banter)            offer (flash/ad)              retire
    ─────────────            ────────────────              ──────
    add_gag(text, punch) ──► offer(contrasting_to) ──────► mark_spoken()
    source_domain="host"     cross-domain + 0.55 silence   traveled=True, pruned
                             salience = punch x recency    (HARD retire-after-1)

Design notes:
  - NO cooldown clause: retire-after-1 (``traveled``) is strictly stronger than a
    cooldown — a gag is gone after a single travel, so it can never re-fire.
  - Cross-domain only: a gag is never offered into a segment of its own domain.
    In v1 every gag is "host" and is only offered into non-host (flash/ad)
    segments, so ``contrasting_to`` is also a forward guard for multi-domain
    seeding (today it only distinguishes host vs non-host).
  - IN-MEMORY only: a verbal gag is a "tonight" thing; an addon restart correctly
    forgets it. Mutations (seed, retire) are driven from the producer's success
    callbacks at QUEUE time, so a discarded segment never plants or burns a gag
    the listener never heard.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from mammamiradio.home.gag_select import weighted_offer

# Mirrors EveningLedger's proven silence tuning (home/evening_memory.py).
VERBAL_GAG_INJECT_PROBABILITY = 0.55  # silence chance — discovered, not announced
_RECENCY_HALFLIFE_SECONDS = 7200.0  # salience halves ~every 2 hours
_RECENCY_LAMBDA = math.log(2) / _RECENCY_HALFLIFE_SECONDS
_DEFAULT_PUNCH = 3.0  # bare-string new_joke back-compat (mid of 1-5)
MAX_LIVE_GAGS = 10  # bound the in-memory ledger


@dataclass
class VerbalGag:
    """One banter-seeded verbal gag eligible to travel once, cross-domain."""

    id: str
    text: str
    source_domain: str
    punch: float
    created_ts: float
    last_spoken_ts: float = 0.0
    traveled: bool = False

    def salience(self, *, now: float) -> float:
        """punch x recency_decay(now - created_ts) — strongest/newest preferred."""
        age = max(0.0, now - self.created_ts)
        return self.punch * math.exp(-_RECENCY_LAMBDA * age)


@dataclass
class VerbalGagLedger:
    """In-memory pool of travelable verbal gags. Best-effort, never raises into audio."""

    gags: dict[str, VerbalGag] = field(default_factory=dict)
    _counter: int = 0

    def add_gag(
        self,
        text: str,
        *,
        now: float,
        punch: float | None = None,
        source_domain: str = "host",
    ) -> None:
        """Seed a gag. No-op on empty text or a duplicate of a live gag."""
        text = (text or "").strip()
        if not text or any(g.text == text for g in self.gags.values()):
            return
        score = _DEFAULT_PUNCH if punch is None else max(1.0, min(5.0, float(punch)))
        self._counter += 1
        gid = f"vg{self._counter}"
        self.gags[gid] = VerbalGag(
            id=gid,
            text=text,
            source_domain=source_domain,
            punch=score,
            created_ts=now,
        )
        self._prune()

    def offer(
        self,
        *,
        now: float,
        contrasting_to: str,
        rng: random.Random | None = None,
    ) -> tuple[str, VerbalGag] | None:
        """Pick one eligible cross-domain gag (or None). Spends nothing."""
        eligible = [
            (gid, gag) for gid, gag in self.gags.items() if not gag.traveled and gag.source_domain != contrasting_to
        ]
        return weighted_offer(
            eligible,
            now=now,
            inject_probability=VERBAL_GAG_INJECT_PROBABILITY,
            weight=lambda gag, n: gag.salience(now=n),
            rng=rng,
        )

    def mark_spoken(self, gag_id: str, *, now: float) -> None:
        """Retire a gag after its one cross-domain travel actually aired."""
        gag = self.gags.get(gag_id)
        if gag is None:
            return
        gag.last_spoken_ts = now
        gag.traveled = True  # hard retire-after-1
        self._prune()

    def _prune(self) -> None:
        """Drop retired gags (dead weight) and cap live gags, evicting oldest."""
        live = {gid: gag for gid, gag in self.gags.items() if not gag.traveled}
        if len(live) > MAX_LIVE_GAGS:
            newest = sorted(live.items(), key=lambda kv: kv[1].created_ts, reverse=True)
            live = dict(newest[:MAX_LIVE_GAGS])
        self.gags = live
